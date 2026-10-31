"""A minimal Dropbox API client for backups: OAuth (PKCE, so no app secret), chunked upload,
folder listing and delete. Plain httpx; only the handful of endpoints the backup needs.

Access tokens are short-lived, so each client gets a fresh one from the long-lived refresh
token on first use. `tracker dropbox-auth` obtains the refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"
AUTHORIZE = "https://www.dropbox.com/oauth2/authorize"
CHUNK = 32 * 1024 * 1024  # upload session chunk; a single upload call is capped at 150 MB


class DropboxError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for the S256 PKCE flow."""
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(app_key: str, challenge: str) -> str:
    """The page where the user approves the app and gets a one-time code to paste back."""
    return AUTHORIZE + "?" + urlencode({
        "client_id": app_key,
        "response_type": "code",
        "token_access_type": "offline",  # also return a refresh token
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:200]}"
    return body.get("error_summary") or body.get("error_description") or str(body.get("error") or body)


def exchange_code(app_key: str, code: str, verifier: str, http: httpx.Client | None = None) -> str:
    """Trade the code from the authorize page for a refresh token."""
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(f"{API}/oauth2/token", data={
            "grant_type": "authorization_code", "code": code.strip(), "client_id": app_key, "code_verifier": verifier,
        })
    finally:
        if http is None:
            client.close()
    if r.status_code != 200:
        raise DropboxError(f"Dropbox did not accept the code: {_error(r)}")
    return r.json()["refresh_token"]


class Dropbox:
    def __init__(self, app_key: str, refresh_token: str, http: httpx.Client | None = None, timeout: float = 300):
        self.app_key = app_key
        self.refresh_token = refresh_token
        self._http = http or httpx.Client(timeout=timeout)
        self._owns_http = http is None
        self._token: str | None = None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "Dropbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- plumbing -----------------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            r = self._http.post(f"{API}/oauth2/token", data={
                "grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.app_key,
            })
            if r.status_code != 200:
                raise DropboxError(
                    f"Dropbox rejected the credentials ({_error(r)}). Check DROPBOX_APP_KEY and DROPBOX_REFRESH_TOKEN."
                )
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, url: str, **kw) -> httpx.Response:
        try:
            r = self._http.post(url, headers={**self._auth(), **kw.pop("headers", {})}, **kw)
        except httpx.HTTPError as e:
            raise DropboxError(f"could not reach Dropbox: {e}") from e
        if r.status_code != 200:
            raise DropboxError(f"{url.rsplit('/2/', 1)[-1]}: {_error(r)}")
        return r

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        return (r.json() or {}) if r.content else {}  # void endpoints answer with nothing or `null`

    def _rpc(self, endpoint: str, args: dict) -> dict:
        return self._json(self._call(f"{API}/2/{endpoint}", json=args))

    def _content(self, endpoint: str, args: dict, data: bytes) -> dict:
        # Dropbox-API-Arg must be header-safe ASCII; json.dumps escapes everything else.
        headers = {"Dropbox-API-Arg": json.dumps(args), "Content-Type": "application/octet-stream"}
        return self._json(self._call(f"{CONTENT}/2/{endpoint}", headers=headers, content=data))

    # --- operations ------------------------------------------------------------------

    def upload(self, local: Path, remote: str) -> dict:
        """Upload a file of any size with an upload session; replaces a file at the same path."""
        session = self._content("files/upload_session/start", {"close": False}, b"")["session_id"]
        offset = 0
        with local.open("rb") as f:
            while chunk := f.read(CHUNK):
                self._content("files/upload_session/append_v2",
                              {"cursor": {"session_id": session, "offset": offset}, "close": False}, chunk)
                offset += len(chunk)
        return self._content("files/upload_session/finish", {
            "cursor": {"session_id": session, "offset": offset},
            "commit": {"path": remote, "mode": "overwrite", "mute": True},
        }, b"")

    def list_files(self, folder: str) -> list[dict]:
        """Files directly in `folder`; an empty list if the folder does not exist yet."""
        try:
            page = self._rpc("files/list_folder", {"path": folder})
        except DropboxError as e:
            if "not_found" in str(e):
                return []
            raise
        entries = page["entries"]
        while page.get("has_more"):
            page = self._rpc("files/list_folder/continue", {"cursor": page["cursor"]})
            entries += page["entries"]
        return [e for e in entries if e.get(".tag") == "file"]

    def delete(self, path: str) -> None:
        """Move to Dropbox's deleted files, from which it can be restored for a while."""
        self._rpc("files/delete_v2", {"path": path})

"""Backups: the archive round trip (export, then restore into changed data, keeping this machine's
paths), rejected uploads, the Dropbox upload with pruning (respx), the web endpoints, and the
daily backup slot in the scheduler."""

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from test_scheduler import Clock, at
from test_web import HEADERS, app_env, wait_idle  # noqa: F401 - shared fixture: app + fixture database
from tracker import backup, db, dropbox
from tracker.dropbox import API, CONTENT, Dropbox, DropboxError
from tracker.web.scheduler import Scheduler


def votes(path) -> int:
    with sqlite3.connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM vote_event").fetchone()[0]


def make_archive(state, tmp) -> Path:
    dest = tmp / "b.zip"
    conn = state.conn()
    backup.create_archive(conn, state.config_path, dest)
    conn.close()
    return dest


# --- archive --------------------------------------------------------------------------


def test_archive_holds_config_database_and_manifest(app_env):
    client, state, cfg, tmp = app_env
    path = make_archive(state, tmp)
    with zipfile.ZipFile(path) as zf:
        assert set(zf.namelist()) == {"manifest.json", "config.toml", "tracker.db"}
        manifest = json.loads(zf.read("manifest.json"))
        assert zf.read("config.toml").decode() == cfg.read_text()
    assert manifest["format"] == "vote-tracker-backup" and manifest["version"] == 1
    assert manifest["tracked"] == ["D000197", "B001267", "H000273"]
    assert manifest["votes"] == votes(tmp / "tracker.db") > 0
    assert not list(tmp.glob("b.zip.db"))  # the snapshot is cleaned up


def test_restore_replaces_data_and_settings_but_keeps_local_paths(app_env, tmp_path_factory):
    client, state, cfg, tmp = app_env
    archive = make_archive(state, tmp)
    before_votes = votes(tmp / "tracker.db")

    # Change both after the backup: drop the votes, change a setting, move the log directory.
    with sqlite3.connect(tmp / "tracker.db") as c:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("DELETE FROM vote_event")
    cfg.write_text(cfg.read_text().replace("top_n = 10", "top_n = 3").replace(f"{tmp}/logs", f"{tmp}/other-logs"))

    work = tmp_path_factory.mktemp("work")
    r = backup.open_archive(archive, work, cfg)
    conn = state.conn()
    stats = backup.restore(conn, r, cfg, tmp / "tracker.db")
    conn.close()

    assert votes(tmp / "tracker.db") == before_votes
    assert votes(tmp / "tracker.db.before-restore") == 0  # the pre-restore data is kept
    text = cfg.read_text()
    assert "top_n = 10" in text and "this comment must survive saves" in text  # the backed-up settings
    assert f"{tmp}/other-logs" in text  # ...but this machine's log_dir
    assert "top_n = 3" in (tmp / "config.toml.bak").read_text()
    assert stats["restored_from"] == r.manifest["created_at"]


def test_keep_local_paths_adds_and_removes_keys():
    backup_text = '[storage]\ndb_path = "/data/tracker.db"  # container\nlog_dir = "/data/logs"\n'
    current = '[storage]\ndb_path = "./tracker.db"\n[digest]\noutput_dir = "./out"\n'
    out = backup.keep_local_paths(backup_text, current)
    assert 'db_path = "./tracker.db"' in out
    assert "log_dir" not in out  # the current config uses the default
    assert 'output_dir = "./out"' in out


def zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize(
    "content,error",
    [
        (b"not a zip", "not a zip file"),
        (zip_bytes({"manifest.json": "{}"}), "no config.toml, tracker.db"),
        (zip_bytes({"manifest.json": '{"format": "other"}', "config.toml": "", "tracker.db": ""}), "unknown format"),
        (zip_bytes({"manifest.json": '{"format": "vote-tracker-backup", "version": 99}', "config.toml": "",
                    "tracker.db": ""}), "newer than this app"),
        (zip_bytes({"manifest.json": '{"format": "vote-tracker-backup", "version": 1}', "config.toml": "[members]\n",
                    "tracker.db": ""}), "members.tracked"),
        (zip_bytes({"manifest.json": '{"format": "vote-tracker-backup", "version": 1}',
                    "config.toml": '[members]\ntracked = ["D000197"]\n', "tracker.db": b"garbage" * 100}),
         "can't be read"),
        (zip_bytes({"manifest.json": '{"format": "vote-tracker-backup", "version": 1}',
                    "config.toml": '[members]\ntracked = ["D000197"]\n', "tracker.db": b""}),
         "not a tracker database"),
    ],
)
def test_open_archive_rejects_bad_uploads(tmp_path, content, error):
    upload = tmp_path / "upload.zip"
    upload.write_bytes(content)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[members]\ntracked = ["D000197"]\n')
    with pytest.raises(backup.BackupError, match=error):
        backup.open_archive(upload, tmp_path, cfg)


# --- Dropbox ----------------------------------------------------------------------------


def mock_dropbox(existing: list[str]):
    """Routes for a token refresh, an upload session and a folder holding `existing` files."""
    respx.post(f"{API}/oauth2/token").respond(json={"access_token": "tok", "expires_in": 14400})
    respx.post(f"{CONTENT}/2/files/upload_session/start").respond(json={"session_id": "s1"})
    appends = respx.post(f"{CONTENT}/2/files/upload_session/append_v2").respond(json=None)

    def finish(request):
        path = json.loads(request.headers["Dropbox-API-Arg"])["commit"]["path"]
        existing.append(path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"path_display": path})

    respx.post(f"{CONTENT}/2/files/upload_session/finish").mock(side_effect=finish)
    respx.post(f"{API}/2/files/list_folder").mock(side_effect=lambda r: httpx.Response(200, json={
        "entries": [{".tag": "file", "name": n, "path_lower": f"/vote-tracker/{n.lower()}"} for n in existing]
        + [{".tag": "folder", "name": "keep-me", "path_lower": "/vote-tracker/keep-me"}],
        "has_more": False,
    }))
    deletes = respx.post(f"{API}/2/files/delete_v2").respond(json={})
    return appends, deletes


@respx.mock
def test_to_dropbox_uploads_in_chunks_and_prunes_old_backups(app_env, monkeypatch):
    client, state, cfg, tmp = app_env
    monkeypatch.setattr(dropbox, "CHUNK", 64 * 1024)
    existing = [f"tracker-backup-2026100{d}T093000Z.zip" for d in range(1, 4)] + ["notes.txt"]
    appends, deletes = mock_dropbox(existing)
    s = state.settings
    s.backup.keep = 2
    conn = state.conn()
    with Dropbox("key", "refresh") as dbx:
        result = backup.to_dropbox(conn, s, cfg, tmp, client=dbx)

    assert result["path"].startswith("/vote-tracker/tracker-backup-")
    assert appends.call_count == -(-result["bytes"] // (64 * 1024)) > 1  # every chunk, in order
    offsets = [json.loads(c.request.headers["Dropbox-API-Arg"])["cursor"]["offset"] for c in appends.calls]
    assert offsets == [i * 64 * 1024 for i in range(len(offsets))]
    # Three old backups plus the new one, keeping two: the two oldest go; notes.txt is not ours.
    deleted = [json.loads(c.request.content)["path"] for c in deletes.calls]
    assert deleted == ["/vote-tracker/tracker-backup-20261001t093000z.zip", "/vote-tracker/tracker-backup-20261002t093000z.zip"]
    assert db.get_sync(conn, backup.LAST_OK_KEY)["cursor"] == result["path"]
    assert db.get_sync(conn, backup.LAST_ERROR_KEY) is None
    conn.close()


@respx.mock
def test_to_dropbox_failure_is_recorded(app_env):
    client, state, cfg, tmp = app_env
    respx.post(f"{API}/oauth2/token").respond(400, json={"error": "invalid_grant", "error_description": "refresh token is malformed"})
    conn = state.conn()
    with Dropbox("key", "bad") as dbx, pytest.raises(DropboxError, match="rejected the credentials"):
        backup.to_dropbox(conn, state.settings, cfg, tmp, client=dbx)
    assert "malformed" in db.get_sync(conn, backup.LAST_ERROR_KEY)["cursor"]
    conn.close()


@respx.mock
def test_list_files_of_a_missing_folder_is_empty():
    respx.post(f"{API}/oauth2/token").respond(json={"access_token": "tok"})
    respx.post(f"{API}/2/files/list_folder").respond(409, json={"error_summary": "path/not_found/.."})
    with Dropbox("key", "refresh") as dbx:
        assert dbx.list_files("/nope") == []


def test_authorize_url_uses_pkce_and_offline_access():
    verifier, challenge = dropbox.pkce_pair()
    url = dropbox.authorize_url("appkey", challenge)
    assert 43 <= len(verifier) <= 128
    assert "token_access_type=offline" in url and f"code_challenge={challenge}" in url and "client_id=appkey" in url


# --- web ----------------------------------------------------------------------------------


def test_export_then_import_through_the_api(app_env):
    client, state, cfg, tmp = app_env
    r = client.get("/api/backup/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="tracker-backup-' in r.headers["content-disposition"]
    archive = r.content
    before = votes(tmp / "tracker.db")

    with sqlite3.connect(tmp / "tracker.db") as c:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("DELETE FROM vote_event")
    put = client.get("/api/config").json()["values"]
    put["digest"]["top_n"] = 4
    assert client.put("/api/config", json={"values": put}, headers=HEADERS).status_code == 200

    assert client.post("/api/backup/import", content=archive).status_code == 403  # client header required
    r = client.post("/api/backup/import", content=archive, headers={**HEADERS, "Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert r.json()["backup"]["votes"] == before
    wait_idle(state)
    job = state.runner.find("restore")
    assert job.status == "ok", job.message
    assert votes(tmp / "tracker.db") == before
    assert state.settings.digest.top_n == 10  # settings reloaded from the restored config
    assert client.get("/api/config").json()["values"]["digest"]["top_n"] == 10
    runs = client.get("/api/jobs").json()["runs"]
    assert runs[0]["command"] == "restore" and runs[0]["status"] == "ok"


def test_import_rejects_a_bad_file_without_touching_anything(app_env):
    client, state, cfg, tmp = app_env
    text = cfg.read_text()
    r = client.post("/api/backup/import", content=b"hello", headers=HEADERS)
    assert r.status_code == 422 and "not a zip" in r.json()["detail"]
    assert cfg.read_text() == text and state.runner.find("restore") is None


def test_backup_now_needs_dropbox_secrets(app_env):
    client, state, *_ = app_env
    r = client.post("/api/backup/dropbox", headers=HEADERS)
    assert r.status_code == 409 and "DROPBOX_APP_KEY" in r.json()["detail"]


def test_status_describes_the_backup(app_env):
    client, state, *_ = app_env
    b = client.get("/api/status").json()["schedule"]["backup"]
    assert b["enabled"] is False and b["folder"] == "/vote-tracker" and b["needs_credentials"] is True
    assert b["last_ok"] is None and b["last_error"] is None


# --- scheduler ----------------------------------------------------------------------------


def test_backup_runs_once_per_daily_slot(app_env, monkeypatch):
    client, state, *_ = app_env
    ran = []
    monkeypatch.setattr("tracker.web.app.submit_backup", lambda st, label: ran.append(label))
    clock = Clock(at(2026, 10, 28, 12, 0))
    scheduler = Scheduler(state, clock=clock)
    assert "backup" not in scheduler.tick()  # off by default
    state.settings.backup.enabled = True
    assert "backup" not in scheduler.tick()  # no Dropbox secrets
    state.settings.secrets.dropbox_app_key, state.settings.secrets.dropbox_refresh_token = "key", "token"
    assert "backup" in scheduler.tick()  # enabling backs up for the latest slot at once
    assert "backup" not in scheduler.tick()
    clock.now = at(2026, 10, 29, 3, 29)
    assert "backup" not in scheduler.tick()
    clock.now = at(2026, 10, 29, 3, 30)
    assert "backup" in scheduler.tick()
    assert ran == ["Scheduled backup to Dropbox"] * 2

"""Backups of all settings and data: one zip with config.toml, a snapshot of the database and a
manifest. Made on demand (Admin → Backup → Export) or daily to Dropbox; restored from an upload.

Secrets are never in a backup: they live only in the environment. Restoring keeps this
machine's own file locations (db_path, log_dir, output_dir) rather than the backed-up ones, so a
backup made on a laptop restores into the Docker container and vice versa.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import tomllib
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import tomlkit
from pydantic import ValidationError

from . import db
from .config import Settings, migrate_config
from .config_store import write_config_text
from .dropbox import Dropbox, DropboxError

log = logging.getLogger(__name__)

FORMAT = "vote-tracker-backup"
VERSION = 1
NAME_RE = re.compile(r"^tracker-backup-\d{8}T\d{6}Z\.zip$")
# Machine-specific paths: a restore keeps the current config's values for these.
LOCAL_KEYS = (("storage", "db_path"), ("storage", "log_dir"), ("storage", "output_dir"))
LAST_OK_KEY = "backup:last_ok"  # cursor: Dropbox path of the last successful backup
LAST_ERROR_KEY = "backup:last_error"  # cursor: why the last attempt failed; removed on success


class BackupError(Exception):
    pass


def archive_name(now: datetime | None = None) -> str:
    return f"tracker-backup-{(now or datetime.now(UTC)).astimezone(UTC):%Y%m%dT%H%M%SZ}.zip"


def _app_version() -> str:
    try:
        return metadata.version("vote-tracker")
    except metadata.PackageNotFoundError:
        return "unknown"


def create_archive(conn: sqlite3.Connection, config_path: Path, dest: Path) -> dict:
    """Write a backup zip to `dest` and return its manifest. The database snapshot is consistent
    even while jobs write to it (VACUUM INTO reads one transaction)."""
    snapshot = dest.with_name(dest.name + ".db")
    snapshot.unlink(missing_ok=True)
    try:
        conn.commit()
        conn.execute("VACUUM INTO ?", (str(snapshot),))
        with closing(sqlite3.connect(str(snapshot))) as snap:
            votes = snap.execute("SELECT COUNT(*) FROM vote_event").fetchone()[0]
        settings = tomllib.loads(config_path.read_text(encoding="utf-8"))
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_at": db.utcnow(),
            "app_version": _app_version(),
            "congress": settings.get("congress", {}).get("number"),
            "tracked": settings.get("members", {}).get("tracked", []),
            "votes": votes,
        }
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.write(config_path, "config.toml")
            zf.write(snapshot, "tracker.db")
    finally:
        snapshot.unlink(missing_ok=True)
    return manifest


# --- restore --------------------------------------------------------------------------


@dataclass
class Restore:
    """An uploaded backup, checked and unpacked, ready to apply."""

    manifest: dict
    config_text: str  # the backed-up config.toml with this machine's local paths
    db_path: Path  # the unpacked database snapshot


def keep_local_paths(backup_text: str, current_text: str) -> str:
    """The backed-up config.toml, comments intact, with LOCAL_KEYS taken from the current one."""
    doc = tomlkit.parse(backup_text)
    current = tomllib.loads(current_text)
    migrate_config(doc)
    migrate_config(current)
    for section, key in LOCAL_KEYS:
        value = current.get(section, {}).get(key)
        table = doc.get(section)
        if value is None:
            if table is not None and key in table:
                del table[key]
        else:
            if table is None:
                table = tomlkit.table()
                doc[section] = table
            table[key] = value
    return tomlkit.dumps(doc)


def open_archive(path: Path, work: Path, current_config: Path) -> Restore:
    """Check an uploaded backup and unpack it into `work`. Raises BackupError saying what is wrong."""
    if not zipfile.is_zipfile(path):
        raise BackupError("That file is not a backup: it is not a zip file.")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        missing = {"manifest.json", "config.toml", "tracker.db"} - names
        if missing:
            raise BackupError(f"That file is not a tracker backup: it has no {', '.join(sorted(missing))}.")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except ValueError:
            raise BackupError("The backup's manifest.json is not valid JSON.") from None
        if manifest.get("format") != FORMAT:
            raise BackupError("That file is not a tracker backup (unknown format).")
        if not isinstance(manifest.get("version"), int) or manifest["version"] > VERSION:
            raise BackupError(
                f"The backup is format version {manifest.get('version')}, newer than this app understands "
                f"({VERSION}). Upgrade the app, then restore."
            )
        backup_text = zf.read("config.toml").decode("utf-8")
        db_path = work / "tracker.db"
        with zf.open("tracker.db") as src, db_path.open("wb") as out:
            while chunk := src.read(1024 * 1024):
                out.write(chunk)

    config_text = keep_local_paths(backup_text, current_config.read_text(encoding="utf-8"))
    try:
        data = tomllib.loads(config_text)
        data.pop("secrets", None)
        Settings.model_validate(data)
    except tomllib.TOMLDecodeError as e:
        raise BackupError(f"The backup's config.toml is not valid TOML: {e}") from None
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise BackupError(f"The backup's settings are not valid for this version of the app: {problems}") from None

    try:
        with closing(sqlite3.connect(str(db_path))) as snap:
            check = snap.execute("PRAGMA quick_check").fetchone()[0]
            tables = {r[0] for r in snap.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        raise BackupError(f"The backup's database can't be read: {e}") from None
    if check != "ok":
        raise BackupError(f"The backup's database is damaged: {check}")
    if not {"raw_record", "vote_event", "sync_state"} <= tables:
        raise BackupError("The backup's database is not a tracker database.")
    return Restore(manifest, config_text, db_path)


def restore(conn: sqlite3.Connection, r: Restore, config_path: Path, live_db: Path) -> dict:
    """Replace the live database and config.toml with the backup's.

    The current database is first saved next to it as <name>.before-restore, and config.toml as
    config.toml.bak. The database is copied in place with SQLite's backup API, so connections
    other threads hold stay valid and see the restored data.
    """
    before = live_db.with_name(live_db.name + ".before-restore")
    before.unlink(missing_ok=True)
    conn.commit()
    conn.execute("VACUUM INTO ?", (str(before),))
    with closing(sqlite3.connect(str(r.db_path))) as src:
        src.backup(conn)
    db.init_db(conn)  # migrate a backup made by an older version
    write_config_text(config_path, r.config_text)
    log.info("restored backup from %s; previous database saved as %s", r.manifest.get("created_at"), before)
    return {"restored_from": r.manifest.get("created_at"), "previous_db": str(before)}


# --- Dropbox ------------------------------------------------------------------------------


def to_dropbox(conn: sqlite3.Connection, s: Settings, config_path: Path, work: Path, client: Dropbox | None = None) -> dict:
    """Make a backup in `work`, upload it to the configured folder, and delete the oldest backups
    beyond `keep`. Only files named like tracker backups are ever deleted."""
    if client is None and not s.secrets.dropbox_ready:
        raise BackupError("DROPBOX_APP_KEY and DROPBOX_REFRESH_TOKEN must be set in the server's environment.")
    folder = s.backup.dropbox_folder
    name = archive_name()
    local = work / name
    try:
        manifest = create_archive(conn, config_path, local)
        size = local.stat().st_size
        dbx = client or Dropbox(s.secrets.dropbox_app_key, s.secrets.dropbox_refresh_token)
        try:
            remote = dbx.upload(local, f"{folder}/{name}")["path_display"]
            ours = sorted((f for f in dbx.list_files(folder) if NAME_RE.match(f["name"])), key=lambda f: f["name"])
            stale = ours[: max(0, len(ours) - s.backup.keep)]
            for f in stale:
                dbx.delete(f["path_lower"])
        finally:
            if client is None:
                dbx.close()
    except (BackupError, DropboxError, OSError, sqlite3.Error) as e:
        db.set_sync(conn, LAST_ERROR_KEY, str(e))
        conn.commit()
        raise
    db.set_sync(conn, LAST_OK_KEY, remote)
    conn.execute("DELETE FROM sync_state WHERE source=?", (LAST_ERROR_KEY,))
    conn.commit()
    log.info("backup uploaded to Dropbox %s (%d bytes); deleted %d old", remote, size, len(stale))
    return {"path": remote, "bytes": size, "votes": manifest["votes"], "deleted": [f["name"] for f in stale]}

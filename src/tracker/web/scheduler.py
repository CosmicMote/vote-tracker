"""Scheduled work for `tracker serve`: a daily data update, the weekly digest email, and a daily
backup to Dropbox.

A thread wakes every 30 seconds and compares the clock with the most recent scheduled
"slot" for each task, in the digest time zone. Each slot is attempted once, recorded in
sync_state, so a failure is reported on the Admin tab rather than retried in a loop, and a
slot missed while the computer slept or the server was stopped runs when it comes back.
Only the most recent slot is ever caught up.

The backup runs only when it is enabled and the Dropbox secrets are set. Unlike the email,
enabling it backs up for the latest slot straight away, which doubles as a test of the setup.

The digest email goes out only when email is enabled and members are chosen. Enabling email
never sends for a slot that has already passed: the first email goes out at the next slot. A
week already emailed (by the schedule, the Admin button or `tracker run-weekly`) is not sent again.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo

from .. import analyze, backup, db, pipeline
from ..config import WEEKDAYS, Settings, email_problems
from ..render import fmt_date

if TYPE_CHECKING:
    from .app import AppState

log = logging.getLogger(__name__)

UPDATE_KEY = "schedule:update"  # cursor: the daily slot last started (UTC ISO)
DIGEST_KEY = "schedule:digest"  # cursor: the weekly slot last started (UTC ISO)
EMAIL_SINCE_KEY = "schedule:email_enabled_since"  # cursor: when email was first seen enabled
BACKUP_KEY = "schedule:backup"  # cursor: the daily backup slot last started (UTC ISO)


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def latest_slot(now: datetime, hhmm: str, weekday: int | None = None) -> datetime:
    """The most recent time at or before `now` (an aware local time) matching HH:MM, on `weekday`
    (0 = Monday) if given, else on any day."""
    h, m = (int(x) for x in hhmm.split(":"))
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if weekday is None:
        return slot if slot <= now else slot - timedelta(days=1)
    slot -= timedelta(days=(slot.weekday() - weekday) % 7)
    return slot if slot <= now else slot - timedelta(days=7)


def digest_period(slot: datetime, tz: str) -> analyze.Period:
    """The week a digest sent at `slot` covers: the last full Monday–Sunday week before it."""
    return analyze.week_containing(slot.date() - timedelta(days=slot.weekday() + 1), tz)


def _cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = db.get_sync(conn, key)
    return row["cursor"] if row else None


class Scheduler:
    def __init__(self, state: "AppState", interval: float = 30.0, clock: Callable[[], datetime] | None = None):
        self.state = state
        self.interval = interval
        self.clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned_no_key = False
        self._warned_no_dropbox = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tracker-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the scheduler must keep running
                log.error("scheduler tick failed: %s", e, exc_info=True)
            self._stop.wait(self.interval)

    def tick(self) -> list[str]:
        """Start whatever is due. Returns the job kinds started (for tests and logs)."""
        s = self.state.settings
        now = self.clock()
        local = now.astimezone(ZoneInfo(s.digest.timezone))
        started: list[str] = []
        with closing(self.state.conn()) as conn:
            if self._update_due(conn, s, local):
                started.append("update")
            if self._digest_due(conn, s, local, now):
                started.append("weekly")
            if self._backup_due(conn, s, local):
                started.append("backup")
        return started

    def _update_due(self, conn: sqlite3.Connection, s: Settings, local: datetime) -> bool:
        if not s.schedule.update_daily_at:
            return False
        if not s.secrets.congress_gov_api_key:
            if not self._warned_no_key:
                log.warning("scheduled data updates are off: CONGRESS_GOV_API_KEY is not set")
                self._warned_no_key = True
            return False
        slot = _utc(latest_slot(local, s.schedule.update_daily_at))
        if _cursor(conn, UPDATE_KEY) == slot:
            return False
        db.set_sync(conn, UPDATE_KEY, slot)
        conn.commit()
        from .app import update_job  # avoid an import cycle

        self.state.runner.submit("update", "Scheduled data update", update_job(None), key="update")
        log.info("scheduled data update started (slot %s)", slot)
        return True

    def _digest_due(self, conn: sqlite3.Connection, s: Settings, local: datetime, now: datetime) -> bool:
        if not s.email.enabled:
            if _cursor(conn, EMAIL_SINCE_KEY) is not None:
                conn.execute("DELETE FROM sync_state WHERE source=?", (EMAIL_SINCE_KEY,))
                conn.commit()
            return False
        since = _cursor(conn, EMAIL_SINCE_KEY)
        if since is None:  # just enabled: wait for the next slot rather than sending for a past one
            since = _utc(now)
            db.set_sync(conn, EMAIL_SINCE_KEY, since)
            conn.commit()
        slot_local = latest_slot(local, s.schedule.digest_at, WEEKDAYS.index(s.schedule.digest_weekday))
        slot = _utc(slot_local)
        if slot < since or _cursor(conn, DIGEST_KEY) == slot:
            return False
        if email_problems(s.email) or not s.members.tracked:
            return False  # not ready; the Admin tab says why, and the slot is tried again once fixed
        period = digest_period(slot_local, s.digest.timezone)
        db.set_sync(conn, DIGEST_KEY, slot)
        conn.commit()
        if pipeline.sent_record(conn, period):
            return False  # already emailed some other way (Admin button, CLI)
        self.state.runner.submit(
            "weekly",
            f"Weekly digest email for week of {fmt_date(period.start)}",
            lambda c, st: pipeline.weekly_digest(c, st, period),
            key=f"weekly:{period.start.isoformat()}",
        )
        log.info("scheduled digest email started for week of %s (slot %s)", period.start, slot)
        return True

    def _backup_due(self, conn: sqlite3.Connection, s: Settings, local: datetime) -> bool:
        if not s.backup.enabled:
            return False
        if not s.secrets.dropbox_ready:
            if not self._warned_no_dropbox:
                log.warning("scheduled backups are off: DROPBOX_APP_KEY and DROPBOX_REFRESH_TOKEN must be set")
                self._warned_no_dropbox = True
            return False
        slot = _utc(latest_slot(local, s.backup.daily_at))
        if _cursor(conn, BACKUP_KEY) == slot:
            return False
        db.set_sync(conn, BACKUP_KEY, slot)
        conn.commit()
        from .app import submit_backup  # avoid an import cycle

        submit_backup(self.state, "Scheduled backup to Dropbox")
        log.info("scheduled backup started (slot %s)", slot)
        return True

    # --- for the Admin tab ------------------------------------------------------------

    def describe(self, conn: sqlite3.Connection) -> dict:
        s = self.state.settings
        tz = ZoneInfo(s.digest.timezone)
        local = self.clock().astimezone(tz)
        fmt = lambda dt: f"{dt:%a} {fmt_date(dt.date())}, {dt:%-I:%M %p %Z}"  # noqa: E731
        update = None
        if s.schedule.update_daily_at:
            nxt = latest_slot(local, s.schedule.update_daily_at) + timedelta(days=1)
            update = {"next": fmt(nxt), "needs_key": not s.secrets.congress_gov_api_key}
        weekday = WEEKDAYS.index(s.schedule.digest_weekday)
        nxt_digest = latest_slot(local, s.schedule.digest_at, weekday) + timedelta(days=7)
        last = conn.execute(
            "SELECT source, cursor, last_run FROM sync_state WHERE source LIKE 'digest_sent:%'"
            " ORDER BY source DESC LIMIT 1"
        ).fetchone()
        last_sent = None
        if last:
            week = date.fromisoformat(last["source"].split(":", 1)[1])
            sent_at = datetime.fromisoformat(last["last_run"].replace("Z", "+00:00")).astimezone(tz)
            last_sent = {"week": fmt_date(week), "to": last["cursor"], "at": fmt(sent_at)}
        return {
            "running": True,
            "update": update,
            "backup": self._describe_backup(conn, local, fmt),
            "digest": {
                "email_enabled": s.email.enabled,
                "when": f"every {s.schedule.digest_weekday.title()} at {nxt_digest:%-I:%M %p}",
                "next": fmt(nxt_digest),
                "covers": fmt_date(digest_period(nxt_digest, s.digest.timezone).start),
                "needs_password": not s.secrets.smtp_password,
                "no_members": not s.members.tracked,
                "problems": [f"{p['loc'][-1]}: {p['msg']}" for p in email_problems(s.email)],
                "last_sent": last_sent,
            },
        }

    def _describe_backup(self, conn: sqlite3.Connection, local: datetime, fmt: Callable[[datetime], str]) -> dict:
        s = self.state.settings
        tz = local.tzinfo
        last_ok = db.get_sync(conn, backup.LAST_OK_KEY)
        last_error = db.get_sync(conn, backup.LAST_ERROR_KEY)
        when = lambda row: fmt(datetime.fromisoformat(row["last_run"].replace("Z", "+00:00")).astimezone(tz))  # noqa: E731
        return {
            "enabled": s.backup.enabled,
            "folder": s.backup.dropbox_folder,
            "keep": s.backup.keep,
            "next": fmt(latest_slot(local, s.backup.daily_at) + timedelta(days=1)),
            "needs_credentials": not s.secrets.dropbox_ready,
            "last_ok": {"path": last_ok["cursor"], "at": when(last_ok)} if last_ok else None,
            "last_error": {"message": last_error["cursor"], "at": when(last_error)} if last_error else None,
        }

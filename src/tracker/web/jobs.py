"""Background jobs for the web app.

One worker thread runs jobs one at a time, so pipeline stages never overlap. Each job opens
its own SQLite connection. Jobs with the same key are not queued twice.
"""

from __future__ import annotations

import itertools
import logging
import queue
import sqlite3
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable

from .. import db
from ..config import Settings
from ..pipeline import StageFailed

log = logging.getLogger(__name__)

JobFn = Callable[[sqlite3.Connection, Settings], dict | None]


@dataclass
class Job:
    id: int
    kind: str
    label: str
    key: str | None
    status: str = "queued"  # queued | running | ok | partial | failed | cancelled
    created_at: str = field(default_factory=db.utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    message: str | None = None
    stats: dict | None = None

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def as_dict(self) -> dict:
        return asdict(self)


class JobRunner:
    def __init__(self, get_settings: Callable[[], Settings], history: int = 50):
        self._get_settings = get_settings
        self._queue: queue.Queue[tuple[Job, JobFn] | None] = queue.Queue()
        self._jobs: deque[Job] = deque(maxlen=history)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._thread = threading.Thread(target=self._work, name="tracker-jobs", daemon=True)
        self._thread.start()

    def submit(self, kind: str, label: str, fn: JobFn, key: str | None = None) -> Job:
        with self._lock:
            if key:
                for j in self._jobs:
                    if j.key == key and j.active:
                        return j
            job = Job(id=next(self._ids), kind=kind, label=label, key=key)
            self._jobs.append(job)
        self._queue.put((job, fn))
        return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def find(self, key: str) -> Job | None:
        """The most recent job with this key, if any."""
        with self._lock:
            for j in reversed(self._jobs):
                if j.key == key:
                    return j
        return None

    def running(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.status == "running"), None)

    def cancel_queued(self, predicate: Callable[[Job], bool]) -> list[Job]:
        """Cancel jobs that have not started yet and match the predicate. A running job always finishes."""
        cancelled = []
        with self._lock:
            for j in self._jobs:
                if j.status == "queued" and predicate(j):
                    j.status, j.finished_at, j.message = "cancelled", db.utcnow(), "Cancelled before it started."
                    cancelled.append(j)
        return cancelled

    def idle(self) -> bool:
        with self._lock:
            return not any(j.active for j in self._jobs)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, fn = item
            with self._lock:
                if job.status == "cancelled":
                    continue
                job.status, job.started_at = "running", db.utcnow()
            conn = None
            try:
                settings = self._get_settings()
                conn = db.connect(settings.storage.db_path)
                db.init_db(conn)
                result = fn(conn, settings) or {}
                job.status = result.get("status", "ok")
                job.stats = result.get("stats")
                job.message = result.get("message")
            except StageFailed as e:
                job.status, job.message = "failed", str(e)
            except Exception as e:  # noqa: BLE001 - reported on the job
                log.error("job %s (%s) failed: %s", job.id, job.kind, e, exc_info=True)
                job.status, job.message = "failed", f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = db.utcnow()
                if conn is not None:
                    conn.close()

"""The pipeline stages, shared by the CLI and the web app.

Each function writes one run_log row and returns its stats. A stage that fails as a whole
raises StageFailed; failures of single records are counted in the stats instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from . import analyze, db, deliver, render
from .config import Settings

log = logging.getLogger("tracker")


class StageFailed(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(f"{stage} failed: {message}")
        self.stage = stage


@contextmanager
def logged_run(conn: sqlite3.Connection, command: str, verbose: bool = False) -> Iterator[dict]:
    """Write a run_log row. The body fills `run` with 'status' and 'stats'; an exception marks it failed."""
    run_id = db.start_run(conn, command)
    run: dict = {"status": "ok", "stats": {}}
    try:
        yield run
    except StageFailed:
        db.finish_run(conn, run_id, "failed", run["stats"])
        raise
    except Exception as e:
        run["stats"]["error"] = str(e)
        db.finish_run(conn, run_id, "failed", run["stats"])
        log.error("%s failed: %s", command, e, exc_info=verbose)
        raise StageFailed(command, str(e)) from e
    db.finish_run(conn, run_id, run["status"], run["stats"])


# --- data version: lets the web app re-analyze a week only when something changed ---------


def data_version(conn: sqlite3.Connection) -> int:
    row = db.get_sync(conn, "data_version")
    return int(row["cursor"]) if row and row["cursor"] else 0


def bump_data_version(conn: sqlite3.Connection) -> None:
    db.set_sync(conn, "data_version", str(data_version(conn) + 1))
    conn.commit()


def analysis_signature(conn: sqlite3.Connection, s: Settings) -> str:
    """Changes whenever the data, the tracked members, or the scoring/digest settings change."""
    basis = {
        "data": data_version(conn),
        "tracked": s.members.tracked,
        "scoring": s.scoring.model_dump(mode="json"),
        "top_n": s.digest.top_n,
        "missed_min": s.digest.missed_vote_min_score,
        "tz": s.digest.timezone,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:16]


# --- stages ---------------------------------------------------------------------


def ingest(
    conn: sqlite3.Connection,
    s: Settings,
    chamber: str | None = None,
    since: date | None = None,
    verbose: bool = False,
) -> dict:
    from .ingest import Sources, run_ingest

    sources = Sources.from_settings(s)
    try:
        with logged_run(conn, "ingest", verbose) as run:
            result = run_ingest(conn, s, sources, chamber=chamber, since=since)
            run["stats"] = result.as_dict()
            run["status"] = result.status
            wanted = [chamber] if chamber else ["house", "senate"]
            if all(f"{c}_votes" in result.failed_sources for c in wanted):
                raise StageFailed("ingest", "every requested chamber failed")
            log.info("ingest %s: %s", result.status, json.dumps(run["stats"], sort_keys=True))
            return run
    finally:
        sources.http.close()


def normalize(conn: sqlite3.Connection, s: Settings, all_records: bool = False, verbose: bool = False) -> dict:
    from .normalize import run_normalize

    with logged_run(conn, "normalize", verbose) as run:
        stats = run_normalize(conn, s, all_records)
        run["stats"] = dict(stats)
        run["status"] = "partial" if any(k.endswith("_errors") for k in stats) else "ok"
        if any(k.endswith("_ok") and v for k, v in stats.items()) or stats.get("house_reclassified"):
            bump_data_version(conn)
        log.info("normalize %s: %s", run["status"], json.dumps(run["stats"], sort_keys=True))
        return run


def analyze_week(conn: sqlite3.Connection, s: Settings, period: analyze.Period, verbose: bool = False) -> dict:
    with logged_run(conn, "analyze", verbose) as run:
        result = analyze.run_analyze(conn, s, period)
        run["stats"] = {"period": [period.start.isoformat(), period.end.isoformat()], **result.stats}
        db.set_sync(conn, f"analyzed:{period.start.isoformat()}", analysis_signature(conn, s))
        conn.commit()
        log.info("analyze %s..%s: %s", period.start, period.end, json.dumps(dict(result.stats), sort_keys=True))
        return run


def analyze_if_stale(conn: sqlite3.Connection, s: Settings, period: analyze.Period) -> bool:
    """Run analyze for the week unless it already ran against the current data and settings."""
    row = db.get_sync(conn, f"analyzed:{period.start.isoformat()}")
    if row and row["cursor"] == analysis_signature(conn, s):
        return False
    analyze_week(conn, s, period)
    return True


def summarize_week(conn: sqlite3.Connection, s: Settings, period: analyze.Period, verbose: bool = False) -> dict:
    from .llm import make_provider
    from .sources.congress_gov import CongressGovClient
    from .sources.http import HttpClient
    from .summarize import run_summarize

    provider = make_provider(s)
    http = HttpClient(s.http.user_agent, s.http.min_interval_seconds, s.http.timeout_seconds, s.http.max_attempts)
    key = s.secrets.congress_gov_api_key
    try:
        with logged_run(conn, "summarize", verbose) as run:
            stats = run_summarize(conn, s, period, provider, CongressGovClient(http, key) if key else None)
            run["stats"] = {"provider": provider.name, "week": period.start.isoformat(), **stats}
            run["status"] = "partial" if stats.get("failed") else "ok"
            log.info("summarize: %s", json.dumps(run["stats"], sort_keys=True))
            return run
    finally:
        http.close()


def digest_week(
    conn: sqlite3.Connection, s: Settings, period: analyze.Period, send: bool, verbose: bool = False
) -> dict:
    """Render the digest file and, if asked and enabled, email it. Delivery failure keeps the file."""
    with logged_run(conn, "digest", verbose) as run:
        subject, path = render.write_digest(conn, s, period)
        run["stats"] = {"path": str(path), "subject": subject, "sent": False}
        if send:
            if not s.email.enabled:
                log.info("email.enabled is false; not sending")
                run["stats"]["note"] = "email disabled"
            else:
                try:
                    deliver.send_digest(s, subject, path)
                    run["stats"]["sent"] = True
                except deliver.DeliveryError as e:
                    log.error("delivery failed: %s", e)
                    run["stats"]["error"] = str(e)
                    run["status"] = "partial"
        return run


def refresh_members(conn: sqlite3.Connection, s: Settings, bioguides: list[str], verbose: bool = False) -> dict:
    """Fetch sponsorships (and their bills) for some tracked members, then normalize."""
    from datetime import UTC, datetime, timedelta

    from .ingest import IngestResult, Sources, ensure_crosswalk, ingest_bills, ingest_members

    sources = Sources.from_settings(s)
    try:
        with logged_run(conn, "refresh-members", verbose) as run:
            if sources.congress is None:
                raise StageFailed("refresh-members", "CONGRESS_GOV_API_KEY is not set")
            now = datetime.now(UTC)
            result = IngestResult()
            crosswalk = ensure_crosswalk(conn, s, sources.http, result, now)
            people = [crosswalk.resolve(b) for b in bioguides]
            cutoff = (now.date() - timedelta(days=s.storage.corrections_window_days)).isoformat()
            ingest_members(conn, sources.congress, people, s.congress.number, result, cutoff)
            ingest_bills(conn, sources.congress, s, result, now)
            run["stats"] = {"members": bioguides, **result.as_dict()}
            run["status"] = result.status
        normalize(conn, s, verbose=verbose)
        return run
    finally:
        sources.http.close()


def digest_path(s: Settings, period: analyze.Period) -> Path:
    return render.digest_path(s, period)


def apply_tracked(conn: sqlite3.Connection, s: Settings) -> None:
    """Set legislator.tracked from the configured members (after a members change)."""
    from .normalize import load_crosswalk, sync_tracked
    from .normalize.context import NormalizeContext

    ctx = NormalizeContext(conn=conn, crosswalk=load_crosswalk(conn, s.congress.number), tracked=set(s.members.tracked))
    sync_tracked(ctx, s.members.tracked)
    conn.commit()

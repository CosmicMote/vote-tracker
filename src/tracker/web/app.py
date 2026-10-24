"""FastAPI app: a JSON API under /api, plus the React UI built into ./static.

The app is meant for one person on localhost and has no login. Two guards keep other
websites open in the same browser from driving it:
- TrustedHostMiddleware rejects requests whose Host is not localhost (DNS rebinding).
- Every POST/PUT/PATCH/DELETE to /api must carry an X-Tracker-Client header, which a
  cross-site page cannot send without a CORS preflight that this app never approves.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import analyze, db, pipeline
from ..analyze import last_name
from ..config import EmailConfig, MembersConfig, Settings, email_problems, load_settings
from ..config_store import save_settings
from ..logs import configure_logging
from ..render import DigestBuilder, fmt_date, render_digest
from ..sources.legislators import Crosswalk, CrosswalkError, Person
from ..summarize import pending_summaries
from .jobs import Job, JobRunner
from .scheduler import Scheduler

STATIC_DIR = Path(__file__).parent / "static"
CLIENT_HEADER = "x-tracker-client"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class UIFiles(StaticFiles):
    """Serve the built UI. index.html is revalidated on every load so a rebuilt UI shows up at
    once; the hashed files under assets/ never change and can be cached for good."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


class AppState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.settings = load_settings(config_path)
        self.runner = JobRunner(lambda: self.settings)
        self.analyze_lock = threading.Lock()
        self.auto_summarized: set[str] = set()
        self._crosswalk: tuple[str, Crosswalk] | None = None

    def reload(self) -> Settings:
        self.settings = load_settings(self.config_path)
        return self.settings

    def conn(self) -> sqlite3.Connection:
        conn = db.connect(self.settings.storage.db_path)
        db.init_db(conn)
        return conn

    def current_crosswalk(self, conn: sqlite3.Connection) -> Crosswalk:
        """Current members only (cheap to parse), cached until the stored file changes."""
        row = db.get_raw(conn, "legislators", "legislators_current", "current")
        if row is None:
            raise HTTPException(409, "No member list yet. Run “Fetch member list” or an update on the Admin tab.")
        if self._crosswalk is None or self._crosswalk[0] != row["payload_hash"]:
            self._crosswalk = (row["payload_hash"], Crosswalk.from_json(row["payload"]))
        return self._crosswalk[1]


# --- helpers ----------------------------------------------------------------------


def congress_start(congress: int) -> date:
    return date(1789 + 2 * (congress - 1), 1, 3)


def week_range(s: Settings) -> tuple[date, date]:
    """Mondays of the first week of the Congress and of the last full week."""
    tz = s.digest.timezone
    first = analyze.week_containing(congress_start(s.congress.number), tz).start
    last = analyze.last_full_week(tz).start
    return first, max(first, last)


def parse_week(s: Settings, week: str) -> analyze.Period:
    try:
        d = date.fromisoformat(week)
    except ValueError:
        raise HTTPException(400, f"Invalid week {week!r}; use YYYY-MM-DD.") from None
    period = analyze.week_containing(d, s.digest.timezone)
    first, last = week_range(s)
    if not first <= period.start <= last:
        raise HTTPException(400, f"Week must be between {first} and {last}.")
    return period


def week_info(p: analyze.Period) -> dict:
    return {"start": p.start.isoformat(), "end": p.end.isoformat(), "label": f"{fmt_date(p.start)} – {fmt_date(p.end)}"}


def person_dict(p: Person) -> dict:
    return {
        "bioguide": p.bioguide,
        "lis": p.lis,
        "name": p.name,
        "chamber": p.chamber,
        "state": p.state,
        "district": p.district,
        "party": p.party,
        "caucus": p.caucus,
    }


def secrets_status(s: Settings) -> dict[str, bool]:
    return {
        "CONGRESS_GOV_API_KEY": bool(s.secrets.congress_gov_api_key),
        "ANTHROPIC_API_KEY": bool(s.secrets.anthropic_api_key),
        "SMTP_PASSWORD": bool(s.secrets.smtp_password),
    }


def validation_detail(e: ValidationError) -> list[dict]:
    """pydantic errors as [{"loc", "msg"}], with field paths that match the settings form."""
    out = []
    for err in e.errors():
        msg = err["msg"].removeprefix("Value error, ")
        loc = [str(x) for x in err["loc"] if not str(x).startswith("function-")]
        out.append({"loc": loc, "msg": msg})
    return out


# --- jobs ---------------------------------------------------------------------------


def submit_summarize(state: AppState, period: analyze.Period) -> Job:
    return state.runner.submit(
        "summarize",
        f"Summarize bills for week of {fmt_date(period.start)}",
        lambda conn, s: pipeline.summarize_week(conn, s, period),
        key=f"summarize:{period.start.isoformat()}",
    )


def submit_refresh_members(state: AppState, bioguides: list[str]) -> Job | None:
    if not bioguides:
        return None
    return state.runner.submit(
        "refresh-members",
        f"Fetch sponsorships for {', '.join(bioguides)}",
        lambda conn, s: pipeline.refresh_members(conn, s, bioguides),
        key=f"refresh-members:{','.join(sorted(bioguides))}",
    )


def update_job(since: date | None):
    """Job function: ingest (optionally backfilling from `since`), then normalize."""
    return lambda conn, s: _update(conn, s, since)


def _update(conn: sqlite3.Connection, s: Settings, since: date | None) -> dict:
    try:
        run = pipeline.ingest(conn, s, since=since)
    except pipeline.StageFailed as e:
        run = {"status": "failed", "stats": {"ingest_error": str(e)}}
    norm = pipeline.normalize(conn, s)
    status = "ok" if run["status"] == "ok" and norm["status"] == "ok" else "partial"
    return {"status": status, "stats": {"ingest": run["stats"], "normalize": norm["stats"]}}


def _crosswalk_job(conn: sqlite3.Connection, s: Settings) -> dict:
    from datetime import UTC, datetime

    from ..ingest import IngestResult, ensure_crosswalk
    from ..sources.http import HttpClient

    http = HttpClient(s.http.user_agent, s.http.min_interval_seconds, s.http.timeout_seconds, s.http.max_attempts)
    try:
        result = IngestResult()
        ensure_crosswalk(conn, s, http, result, datetime.now(UTC), force=True)
        return {"status": "ok", "stats": result.as_dict()}
    finally:
        http.close()


# --- request models -------------------------------------------------------------------


class MembersIn(BaseModel):
    tracked: list[str]


class ConfigIn(BaseModel):
    values: dict


class JobIn(BaseModel):
    kind: Literal["update", "ingest", "normalize", "summarize", "crosswalk"]
    week: str | None = None
    since: str | None = None
    all_records: bool = False


# --- app ------------------------------------------------------------------------------


def create_app(
    config_path: Path | str | None = None,
    allowed_hosts: list[str] | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    path = Path(config_path or os.environ.get("TRACKER_CONFIG", "config.toml")).resolve()
    hosts = allowed_hosts or [h for h in os.environ.get("TRACKER_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h]
    state = AppState(path)
    configure_logging(state.settings)

    scheduler = Scheduler(state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()
        state.runner.shutdown()

    app = FastAPI(title="Legislator Vote Tracker", lifespan=lifespan)
    app.state.tracker = state
    app.state.scheduler = scheduler
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def require_client_header(request: Request, call_next):
        if request.method in MUTATING and request.url.path.startswith("/api/") and not request.headers.get(CLIENT_HEADER):
            return JSONResponse({"detail": f"Missing {CLIENT_HEADER} header."}, status_code=403)
        return await call_next(request)

    # --- status and weeks ---------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        s = state.settings
        first, last = week_range(s)
        with closing(state.conn()) as conn:
            stale = DigestBuilder(conn, s, analyze.week_containing(last, s.digest.timezone)).stale_warnings()
            counts = {
                r["chamber"]: {"votes": r["n"], "latest": r["latest"]}
                for r in conn.execute("SELECT chamber, COUNT(*) n, MAX(vote_date) latest FROM vote_event GROUP BY chamber")
            }
            has_crosswalk = db.get_raw(conn, "legislators", "legislators_current", "current") is not None
            schedule = {**scheduler.describe(conn), "running": start_scheduler}
        return {
            "config_path": str(state.config_path),
            "db_path": str(s.storage.db_path),
            "congress": s.congress.number,
            "provider": f"{s.llm.provider}:{s.llm.model}",
            "email_enabled": s.email.enabled,
            "secrets": secrets_status(s),
            "stale": stale,
            "votes": counts,
            "has_crosswalk": has_crosswalk,
            "busy": not state.runner.idle(),
            "weeks": {"first": first.isoformat(), "last": last.isoformat()},
            "schedule": schedule,
        }

    @app.get("/api/weeks")
    def weeks() -> dict:
        s = state.settings
        first, last = week_range(s)
        out, d = [], last
        while d >= first:
            out.append(week_info(analyze.week_containing(d, s.digest.timezone)))
            d -= timedelta(days=7)
        return {"default": last.isoformat(), "first": first.isoformat(), "last": last.isoformat(), "weeks": out}

    # --- digest ---------------------------------------------------------------

    def analyzed_digest(period: analyze.Period) -> tuple[dict, list[int], bool]:
        s = state.settings
        with closing(state.conn()) as conn:
            with state.analyze_lock:
                reanalyzed = pipeline.analyze_if_stale(conn, s, period)
            ctx = DigestBuilder(conn, s, period).build()
            pending = pending_summaries(conn, period, s)
        return ctx, pending, reanalyzed

    @app.get("/api/digest/{week}")
    def digest(week: str) -> dict:
        s = state.settings
        period = parse_week(s, week)
        ctx, pending, reanalyzed = analyzed_digest(period)
        key = f"summarize:{period.start.isoformat()}"
        # The week being viewed comes first: summaries for other weeks that have not started yet
        # are dropped (a running one finishes), and are queued again when those weeks are opened.
        for dropped in state.runner.cancel_queued(lambda j: j.kind == "summarize" and j.key != key):
            week_start = (dropped.key or "").removeprefix("summarize:")
            state.auto_summarized = {k for k in state.auto_summarized if not k.startswith(f"{week_start}:")}
        job = state.runner.find(key)
        # Start summaries automatically once per week and model; after that, the user retries.
        auto_key = f"{period.start}:{s.llm.provider}:{s.llm.model}:{s.llm.prompt_version}"
        if pending and (job is None or not job.active) and auto_key not in state.auto_summarized:
            state.auto_summarized.add(auto_key)
            job = submit_summarize(state, period)
        ahead = state.runner.running() if job is not None and job.status == "queued" else None
        return {
            "week": week_info(period),
            "digest": ctx,
            "reanalyzed": reanalyzed,
            "summaries": {
                "pending": len(pending),
                "job": job.as_dict() if job else None,
                "waiting_for": ahead.label if ahead else None,
            },
        }

    @app.get("/api/digest/{week}/email", response_class=HTMLResponse)
    def digest_email(week: str) -> HTMLResponse:
        s = state.settings
        period = parse_week(s, week)
        with closing(state.conn()) as conn:
            with state.analyze_lock:
                pipeline.analyze_if_stale(conn, s, period)
            _, html = render_digest(conn, s, period)
        return HTMLResponse(html)

    @app.post("/api/digest/{week}/summarize")
    def digest_summarize(week: str) -> dict:
        period = parse_week(state.settings, week)
        return submit_summarize(state, period).as_dict()

    @app.post("/api/digest/{week}/send")
    def digest_send(week: str) -> dict:
        s = state.settings
        period = parse_week(s, week)
        if not s.email.enabled:
            raise HTTPException(409, "Email is disabled. Enable it on the Admin tab first.")
        if problems := email_problems(s.email):
            raise HTTPException(409, "Email settings are incomplete: " + "; ".join(
                f"{p['loc'][-1]} {p['msg']}" for p in problems))
        if not s.secrets.smtp_password:
            raise HTTPException(409, "SMTP_PASSWORD is not set in the server's environment.")
        def send(conn: sqlite3.Connection, st: Settings) -> dict:
            with state.analyze_lock:
                pipeline.analyze_if_stale(conn, st, period)
            return pipeline.digest_week(conn, st, period, send=True)

        job = state.runner.submit(
            "send",
            f"Email digest for week of {fmt_date(period.start)} to {s.email.to_addr}",
            send,
            key=f"send:{period.start.isoformat()}",
        )
        return job.as_dict()

    # --- members --------------------------------------------------------------

    @app.get("/api/legislators")
    def legislators() -> dict:
        with closing(state.conn()) as conn:
            xw = state.current_crosswalk(conn)
        people = sorted(
            xw.current.values(),
            key=lambda p: (
                p.state,
                p.chamber != "senate",
                int(p.district) if p.district and p.district.isdigit() else 0,
                last_name(p.name),
            ),
        )
        return {"legislators": [person_dict(p) for p in people]}

    def members_payload(s: Settings, conn: sqlite3.Connection) -> dict:
        xw = state.current_crosswalk(conn)
        resolved, missing = [], []
        for b in s.members.tracked:
            p = xw.current.get(b)
            if p:
                resolved.append(person_dict(p))
            else:
                missing.append(b)
        return {"tracked": s.members.tracked, "members": resolved, "missing": missing}

    @app.get("/api/members")
    def get_members() -> dict:
        with closing(state.conn()) as conn:
            return members_payload(state.settings, conn)

    def save_and_apply(new: Settings, old: Settings) -> Job | None:
        """Persist settings, reload them, and react to a change in tracked members."""
        save_settings(state.config_path, new)
        s = state.reload()
        added = [b for b in s.members.tracked if b not in old.members.tracked]
        if s.members.tracked != old.members.tracked:
            with closing(state.conn()) as conn:
                pipeline.apply_tracked(conn, s)
        return submit_refresh_members(state, added) if added and s.secrets.congress_gov_api_key else None

    @app.put("/api/members")
    def put_members(body: MembersIn) -> dict:
        old = state.settings
        try:
            tracked = MembersConfig(tracked=body.tracked).tracked
        except ValidationError as e:
            raise HTTPException(422, validation_detail(e)) from None
        with closing(state.conn()) as conn:
            xw = state.current_crosswalk(conn)
            try:
                for b in tracked:
                    xw.resolve(b)
            except CrosswalkError as e:
                raise HTTPException(422, str(e)) from None
        data = old.model_dump(mode="json")
        data["members"]["tracked"] = tracked
        job = save_and_apply(Settings.model_validate(data), old)
        with closing(state.conn()) as conn:
            payload = members_payload(state.settings, conn)
        return {**payload, "job": job.as_dict() if job else None}

    # --- config ---------------------------------------------------------------

    def config_payload() -> dict:
        s = state.settings
        return {
            "path": str(state.config_path),
            "values": s.model_dump(mode="json", exclude={"secrets"}),
            "schema": Settings.model_json_schema(),
            "secrets": secrets_status(s),
        }

    @app.get("/api/config")
    def get_config() -> dict:
        return config_payload()

    def check_values(values: dict) -> tuple[Settings | None, list[dict]]:
        """Everything that must hold before settings are saved: the config model's own rules,
        email readiness, and tracked members that resolve to current members of Congress."""
        try:
            new = Settings.model_validate(values)
        except ValidationError as e:
            errors = validation_detail(e)
            try:  # still report email readiness, so every problem shows at once
                errors += email_problems(EmailConfig.model_validate(values.get("email") or {}))
            except ValidationError:
                pass  # the email section's own errors are already listed
            return None, errors
        errors = email_problems(new.email)
        if new.members.tracked != state.settings.members.tracked:
            with closing(state.conn()) as conn:
                xw = state.current_crosswalk(conn)
                for b in new.members.tracked:
                    try:
                        xw.resolve(b)
                    except CrosswalkError as e:
                        errors.append({"loc": ["members", "tracked"], "msg": str(e)})
        return new, errors

    @app.post("/api/config/validate")
    def validate_config(body: ConfigIn) -> dict:
        """Check settings without saving them; the Admin tab calls this as you type."""
        return {"errors": check_values(body.values)[1]}

    @app.put("/api/config")
    def put_config(body: ConfigIn) -> dict:
        old = state.settings
        new, errors = check_values(body.values)
        if errors or new is None:
            raise HTTPException(422, errors)
        job = save_and_apply(new, old)
        return {**config_payload(), "job": job.as_dict() if job else None}

    # --- jobs -----------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        with closing(state.conn()) as conn:
            runs = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, command, started_at, finished_at, status, stats_json FROM run_log ORDER BY id DESC LIMIT 30"
                )
            ]
        return {"jobs": [j.as_dict() for j in state.runner.jobs()], "runs": runs}

    @app.post("/api/jobs")
    def create_job(body: JobIn) -> dict:
        s = state.settings
        since = None
        if body.since:
            try:
                since = date.fromisoformat(body.since)
            except ValueError:
                raise HTTPException(400, "since must be YYYY-MM-DD") from None
        if body.kind in ("update", "ingest") and not s.secrets.congress_gov_api_key:
            raise HTTPException(409, "CONGRESS_GOV_API_KEY is not set in the server's environment.")
        if body.kind == "update":
            job = state.runner.submit(
                "update", "Fetch new data and normalize" + (f" (backfill from {since})" if since else ""),
                update_job(since), key="update",
            )
        elif body.kind == "ingest":
            job = state.runner.submit("ingest", "Fetch new data", lambda conn, st: pipeline.ingest(conn, st, since=since), key="update")
        elif body.kind == "normalize":
            job = state.runner.submit(
                "normalize", "Normalize" + (" (all records)" if body.all_records else ""),
                lambda conn, st: pipeline.normalize(conn, st, body.all_records), key="normalize",
            )
        elif body.kind == "crosswalk":
            job = state.runner.submit("crosswalk", "Fetch member list (congress-legislators)", _crosswalk_job, key="crosswalk")
        else:
            if not body.week:
                raise HTTPException(400, "summarize needs a week")
            job = submit_summarize(state, parse_week(s, body.week))
        return job.as_dict()

    # --- UI -------------------------------------------------------------------

    if (STATIC_DIR / "index.html").exists():
        app.mount("/", UIFiles(directory=STATIC_DIR, html=True), name="ui")
    else:

        @app.get("/", response_class=HTMLResponse)
        def ui_missing() -> str:
            return (
                "<!doctype html><title>Vote Tracker</title><body style='font-family:sans-serif;padding:2rem'>"
                "<h1>The web UI has not been built</h1><p>Run <code>npm --prefix frontend install</code> and "
                "<code>npm --prefix frontend run build</code>, then restart <code>tracker serve</code>. "
                "The API is available under <a href='/docs'>/docs</a>.</p></body>"
            )

    return app

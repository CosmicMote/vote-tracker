"""`tracker` command line. Every command writes a run_log row; each stage is safe to re-run."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

import typer

from . import analyze, db, pipeline
from .config import Settings, load_settings
from .logs import configure_logging

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Weekly digest of your congressional delegation.")
log = logging.getLogger("tracker")


class State:
    config_path: Optional[Path] = None
    verbose: bool = False


@app.callback()
def main(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config.toml (default ./config.toml)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    State.config_path = config
    State.verbose = verbose


def setup_logging(settings: Settings) -> None:
    configure_logging(settings, State.verbose)


def settings() -> Settings:
    try:
        s = load_settings(State.config_path)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from None
    setup_logging(s)
    return s


def open_db(s: Settings) -> sqlite3.Connection:
    conn = db.connect(s.storage.db_path)
    db.init_db(conn)
    return conn


@contextmanager
def cli_stage() -> Iterator[None]:
    """Turn a failed pipeline stage into exit status 1 (the stage already logged why)."""
    try:
        yield
    except pipeline.StageFailed as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


def logged_run(conn: sqlite3.Connection, command: str):
    return pipeline.logged_run(conn, command, State.verbose)


def parse_period(s: Settings, week: Optional[str]) -> analyze.Period:
    if week:
        return analyze.week_containing(date.fromisoformat(week), s.digest.timezone)
    return analyze.last_full_week(s.digest.timezone)


WEEK_HELP = "Any date in the target week (YYYY-MM-DD). Default: the last full Monday–Sunday week."


# --- commands ---------------------------------------------------------------


@app.command("init-db")
def init_db_cmd() -> None:
    """Create the database from schema.sql; safe to re-run."""
    s = settings()
    conn = open_db(s)
    with cli_stage(), logged_run(conn, "init-db"):
        pass
    typer.echo(f"database ready at {s.storage.db_path}")


@app.command()
def ingest(
    chamber: Optional[str] = typer.Option(None, help="house or senate (default: both)."),
    since: Optional[str] = typer.Option(None, help="Backfill votes from this date (YYYY-MM-DD)."),
) -> None:
    """Stage 1: fetch new and changed records into raw_record."""
    if chamber not in (None, "house", "senate"):
        raise typer.BadParameter("chamber must be house or senate")
    s = settings()
    with cli_stage():
        pipeline.ingest(open_db(s), s, chamber, date.fromisoformat(since) if since else None, State.verbose)


@app.command()
def normalize(all_records: bool = typer.Option(False, "--all", help="Re-parse every raw record.")) -> None:
    """Stage 2: parse raw records into the relational tables."""
    s = settings()
    with cli_stage():
        pipeline.normalize(open_db(s), s, all_records, State.verbose)


@app.command("analyze")
def analyze_cmd(week: Optional[str] = typer.Option(None, help=WEEK_HELP)) -> None:
    """Stage 3: caucus positions, findings, scoring and selection for a week."""
    s = settings()
    with cli_stage():
        pipeline.analyze_week(open_db(s), s, parse_period(s, week), State.verbose)


@app.command()
def summarize(week: Optional[str] = typer.Option(None, help=WEEK_HELP)) -> None:
    """Stage 4: LLM summaries for bills attached to the week's selected findings."""
    s = settings()
    with cli_stage():
        pipeline.summarize_week(open_db(s), s, parse_period(s, week), State.verbose)


def report_digest(s: Settings, run: dict) -> None:
    typer.echo(f"wrote {run['stats']['path']}")
    if run["stats"]["sent"]:
        typer.echo(f"sent to {s.email.to_addr}")
    elif note := run["stats"].get("note"):
        typer.echo(f"not sent: {note}")


@app.command()
def digest(
    week: Optional[str] = typer.Option(None, help=WEEK_HELP),
    send: bool = typer.Option(False, "--send", help="Email the digest if [email] is enabled."),
) -> None:
    """Stages 5 and 6: render the digest HTML, and optionally email it."""
    s = settings()
    with cli_stage():
        report_digest(s, pipeline.digest_week(open_db(s), s, parse_period(s, week), send, State.verbose))


@app.command("run-weekly")
def run_weekly(
    force: bool = typer.Option(False, "--force", help="Send even if this week's digest was already emailed."),
) -> None:
    """All stages for the last full week, then send (once per week unless --force)."""
    s = settings()
    conn = open_db(s)
    period = analyze.last_full_week(s.digest.timezone)
    log.info("weekly run for %s..%s", period.start, period.end)
    with cli_stage():
        report_digest(s, pipeline.weekly_digest(conn, s, period, force, State.verbose))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind. Keep the default: the app has no login."),
    port: int = typer.Option(8000, help="Port to listen on."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)."),
) -> None:
    """Run the web app (UI + API) at http://HOST:PORT."""
    import os

    import uvicorn

    s = settings()  # fail early on a bad config
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo("warning: the web app has no authentication; binding beyond localhost exposes it", err=True)
    os.environ["TRACKER_CONFIG"] = str((State.config_path or Path("config.toml")).resolve())
    os.environ["TRACKER_ALLOWED_HOSTS"] = ",".join({"127.0.0.1", "localhost", host})
    typer.echo(f"serving on http://{host}:{port}  (database {s.storage.db_path})")
    uvicorn.run("tracker.web.app:create_app", factory=True, host=host, port=port, reload=reload, log_config=None)


@app.command()
def members() -> None:
    """Show tracked members as resolved: chamber, state, Bioguide and LIS IDs."""
    from .ingest import IngestResult, ensure_crosswalk, resolve_tracked
    from .sources.http import HttpClient
    from .sources.legislators import CrosswalkError

    s = settings()
    conn = open_db(s)
    http = HttpClient(s.http.user_agent, s.http.min_interval_seconds, s.http.timeout_seconds, s.http.max_attempts)
    with cli_stage(), logged_run(conn, "members") as run:
        try:
            crosswalk = ensure_crosswalk(conn, s, http, IngestResult(), datetime.now().astimezone())
            people = resolve_tracked(crosswalk, s.members.tracked)
        except CrosswalkError as e:
            raise pipeline.StageFailed("members", str(e)) from None
        finally:
            http.close()
        run["stats"] = {"resolved": len(people)}
    typer.echo(f"{'Bioguide':<9} {'LIS':<5} {'Chamber':<7} {'State':<5} {'Dist':<4} {'Party':<5} {'Caucus':<6} Name")
    for p in people:
        typer.echo(
            f"{p.bioguide:<9} {p.lis or '-':<5} {p.chamber:<7} {p.state:<5} {p.district or '-':<4} "
            f"{p.party:<5} {p.caucus:<6} {p.name}"
        )


@app.command("explain-vote")
def explain_vote(
    chamber: str = typer.Argument(..., help="house or senate"),
    session: str = typer.Argument(..., help="Session, e.g. 119-1 (or just 1 for the configured Congress)"),
    roll: int = typer.Argument(..., help="Roll-call number"),
) -> None:
    """Print classification, threshold, caucus positions and score for one vote."""
    s = settings()
    if "-" not in session:
        session = f"{s.congress.number}-{session}"
    conn = open_db(s)
    with cli_stage(), logged_run(conn, "explain-vote") as run:
        run["stats"] = {"vote": f"{chamber}/{session}/{roll}"}
        try:
            info = analyze.explain_vote(conn, s, chamber, session, roll)
        except LookupError as e:
            raise pipeline.StageFailed("explain-vote", str(e)) from None
    v = info["vote"]
    typer.echo(f"{chamber.title()} {session} roll {roll} — {v['vote_date']}")
    typer.echo(f"  question:   {v['question']}")
    if v["vote_title"]:
        typer.echo(f"  title:      {v['vote_title']}")
    typer.echo(f"  class:      {v['vote_class']} (base weight {info['base_weight']})")
    typer.echo(f"  threshold:  {v['threshold']}")
    typer.echo(f"  result:     {v['result']}  {v['yea']}-{v['nay']} (present {v['present']}, not voting {v['not_voting']})")
    if v["tie_breaker"]:
        typer.echo(f"  tie breaker: Vice President voted {v['tie_breaker']}")
    typer.echo(f"  yea share:  {info['yea_share']}   closeness: {info['closeness']}   lopsided: {info['lopsided']}")
    typer.echo("  caucus positions:")
    for cp in info["caucus_positions"]:
        coh = f"{cp['cohesion']:.3f}" if cp["cohesion"] is not None else "-"
        typer.echo(f"    {cp['caucus']:<3} yea {cp['yea']:>3}  nay {cp['nay']:>3}  majority {cp['majority'] or 'tie':<4}  cohesion {coh}")
    for m in info["members"]:
        if m["position"] is None:
            typer.echo(f"  {m['name']}: no recorded position (not seated)")
        else:
            flags = [f for f, on in (("PARTY BREAK", m["party_break"]), ("sponsor/cosponsor", m["sponsored"])) if on]
            typer.echo(f"  {m['name']}: {m['position']} (caucus {m['caucus']}) score {m['score']} {' '.join(flags)}")


if __name__ == "__main__":
    app()

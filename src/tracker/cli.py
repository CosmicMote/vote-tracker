"""`tracker` command line. Every command writes a run_log row; each stage is safe to re-run."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional

import typer

from . import analyze, db, deliver, render
from .config import Settings, load_settings

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
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if State.verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)
    settings.storage.log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(settings.storage.log_dir / "tracker.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would include the API key


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
def logged_run(conn: sqlite3.Connection, command: str) -> Iterator[dict]:
    """Write a run_log row. The body fills `run` with 'status' and 'stats'; an exception marks it failed."""
    run_id = db.start_run(conn, command)
    run: dict = {"status": "ok", "stats": {}}
    try:
        yield run
    except typer.Exit:
        db.finish_run(conn, run_id, run.get("status", "failed"), run["stats"])
        raise
    except Exception as e:
        run["stats"]["error"] = str(e)
        db.finish_run(conn, run_id, "failed", run["stats"])
        log.error("%s failed: %s", command, e, exc_info=State.verbose)
        raise typer.Exit(1) from None
    db.finish_run(conn, run_id, run["status"], run["stats"])


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
    with logged_run(conn, "init-db"):
        pass
    typer.echo(f"database ready at {s.storage.db_path}")


def do_ingest(conn: sqlite3.Connection, s: Settings, chamber: Optional[str], since: Optional[date]) -> str:
    from .ingest import Sources, run_ingest

    sources = Sources.from_settings(s)
    try:
        with logged_run(conn, "ingest") as run:
            result = run_ingest(conn, s, sources, chamber=chamber, since=since)
            run["stats"] = result.as_dict()
            run["status"] = result.status
            wanted = [chamber] if chamber else ["house", "senate"]
            if all(f"{c}_votes" in result.failed_sources for c in wanted):
                run["status"] = "failed"
                log.error("ingest failed for every requested chamber")
                raise typer.Exit(1)
            log.info("ingest %s: %s", result.status, json.dumps(run["stats"], sort_keys=True))
            return result.status
    finally:
        sources.http.close()


@app.command()
def ingest(
    chamber: Optional[str] = typer.Option(None, help="house or senate (default: both)."),
    since: Optional[str] = typer.Option(None, help="Backfill votes from this date (YYYY-MM-DD)."),
) -> None:
    """Stage 1: fetch new and changed records into raw_record."""
    if chamber not in (None, "house", "senate"):
        raise typer.BadParameter("chamber must be house or senate")
    s = settings()
    do_ingest(open_db(s), s, chamber, date.fromisoformat(since) if since else None)


def do_normalize(conn: sqlite3.Connection, s: Settings, all_records: bool) -> None:
    from .normalize import run_normalize

    with logged_run(conn, "normalize") as run:
        stats = run_normalize(conn, s, all_records)
        run["stats"] = dict(stats)
        run["status"] = "partial" if any(k.endswith("_errors") for k in stats) else "ok"
        log.info("normalize %s: %s", run["status"], json.dumps(run["stats"], sort_keys=True))


@app.command()
def normalize(all_records: bool = typer.Option(False, "--all", help="Re-parse every raw record.")) -> None:
    """Stage 2: parse raw records into the relational tables."""
    s = settings()
    do_normalize(open_db(s), s, all_records)


def do_analyze(conn: sqlite3.Connection, s: Settings, period: analyze.Period) -> None:
    with logged_run(conn, "analyze") as run:
        result = analyze.run_analyze(conn, s, period)
        run["stats"] = {"period": [period.start.isoformat(), period.end.isoformat()], **result.stats}
        log.info("analyze %s..%s: %s", period.start, period.end, json.dumps(dict(result.stats), sort_keys=True))


@app.command("analyze")
def analyze_cmd(week: Optional[str] = typer.Option(None, help=WEEK_HELP)) -> None:
    """Stage 3: caucus positions, findings, scoring and selection for a week."""
    s = settings()
    do_analyze(open_db(s), s, parse_period(s, week))


def do_summarize(conn: sqlite3.Connection, s: Settings, period: analyze.Period) -> None:
    from .llm import make_provider
    from .sources.congress_gov import CongressGovClient
    from .sources.http import HttpClient
    from .summarize import run_summarize

    provider = make_provider(s)
    http = HttpClient(s.http.user_agent, s.http.min_interval_seconds, s.http.timeout_seconds, s.http.max_attempts)
    key = s.secrets.congress_gov_api_key
    try:
        with logged_run(conn, "summarize") as run:
            stats = run_summarize(conn, s, period, provider, CongressGovClient(http, key) if key else None)
            run["stats"] = {"provider": provider.name, **stats}
            run["status"] = "partial" if stats.get("failed") else "ok"
            log.info("summarize: %s", json.dumps(run["stats"], sort_keys=True))
    finally:
        http.close()


@app.command()
def summarize(week: Optional[str] = typer.Option(None, help=WEEK_HELP)) -> None:
    """Stage 4: LLM summaries for bills attached to the week's selected findings."""
    s = settings()
    do_summarize(open_db(s), s, parse_period(s, week))


def do_digest(conn: sqlite3.Connection, s: Settings, period: analyze.Period, send: bool) -> Path:
    with logged_run(conn, "digest") as run:
        subject, path = render.write_digest(conn, s, period)
        run["stats"] = {"path": str(path), "subject": subject, "sent": False}
        typer.echo(f"wrote {path}")
        if send:
            if not s.email.enabled:
                log.info("email.enabled is false; not sending")
            else:
                try:
                    deliver.send_digest(s, subject, path)
                    run["stats"]["sent"] = True
                    typer.echo(f"sent to {s.email.to_addr}")
                except deliver.DeliveryError as e:
                    # The rendered file stays on disk; delivery is retried by re-running digest --send.
                    log.error("delivery failed: %s", e)
                    run["status"] = "partial"
    return path


@app.command()
def digest(
    week: Optional[str] = typer.Option(None, help=WEEK_HELP),
    send: bool = typer.Option(False, "--send", help="Email the digest if [email] is enabled."),
) -> None:
    """Stages 5 and 6: render the digest HTML, and optionally email it."""
    s = settings()
    do_digest(open_db(s), s, parse_period(s, week), send)


@app.command("run-weekly")
def run_weekly() -> None:
    """All stages for the last full week, then send."""
    s = settings()
    conn = open_db(s)
    period = analyze.last_full_week(s.digest.timezone)
    log.info("weekly run for %s..%s", period.start, period.end)
    try:
        do_ingest(conn, s, None, None)
    except typer.Exit:
        log.error("ingest failed; continuing with the data already stored")
    do_normalize(conn, s, False)
    do_analyze(conn, s, period)
    try:
        do_summarize(conn, s, period)
    except typer.Exit:
        log.error("summarize failed; the digest will use fallback summaries")
    do_digest(conn, s, period, send=True)


@app.command()
def members() -> None:
    """Show tracked members as resolved: chamber, state, Bioguide and LIS IDs."""
    from .ingest import ensure_crosswalk, resolve_tracked, IngestResult
    from .sources.http import HttpClient
    from .sources.legislators import CrosswalkError

    s = settings()
    conn = open_db(s)
    http = HttpClient(s.http.user_agent, s.http.min_interval_seconds, s.http.timeout_seconds, s.http.max_attempts)
    with logged_run(conn, "members") as run:
        try:
            crosswalk = ensure_crosswalk(conn, s, http, IngestResult(), datetime.now().astimezone())
            people = resolve_tracked(crosswalk, s.members.tracked)
        except CrosswalkError as e:
            run["status"] = "failed"
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from None
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
    with logged_run(conn, "explain-vote") as run:
        run["stats"] = {"vote": f"{chamber}/{session}/{roll}"}
        try:
            info = analyze.explain_vote(conn, s, chamber, session, roll)
        except LookupError as e:
            run["status"] = "failed"
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from None
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

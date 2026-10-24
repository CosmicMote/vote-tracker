"""Stage 2: parse raw records into the relational tables. Safe to re-run."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from typing import Callable

from .. import classify, db
from ..config import Settings
from ..sources.legislators import Crosswalk, CrosswalkError
from . import bills, house, senate, voteview
from .common import is_rule_title
from .context import NormalizeContext

log = logging.getLogger(__name__)


class Incomplete(Exception):
    """A record that cannot be normalized until another record arrives; retried next run."""


def load_crosswalk(conn: sqlite3.Connection, congress: int = 119) -> Crosswalk | None:
    row = db.get_raw(conn, "legislators", "legislators_current", "current")
    if row is None:
        return None
    hist = db.get_raw(conn, "legislators", "legislators_historical", "historical")
    # Former members matter only if they served in this Congress (which began on Jan 3 of an odd year).
    start_year = 1789 + 2 * (congress - 1)
    return Crosswalk.from_json(row["payload"], hist["payload"] if hist else None, since=f"{start_year}-01-03")


def sync_tracked(ctx: NormalizeContext, tracked: list[str]) -> None:
    """Upsert configured members from the crosswalk and set the tracked flag on exactly them."""
    if ctx.crosswalk is None:
        raise CrosswalkError("No crosswalk in the database; run `tracker ingest` first")
    ids = []
    for bioguide in tracked:
        person = ctx.crosswalk.resolve(bioguide)
        ids.append(ctx.legislator_from_person(person))
    marks = ",".join("?" for _ in ids)
    ctx.conn.execute(f"UPDATE legislator SET tracked = CASE WHEN id IN ({marks}) THEN 1 ELSE 0 END", ids)


def _each(
    ctx: NormalizeContext,
    rows: list[sqlite3.Row],
    label: str,
    fn: Callable[[sqlite3.Row], object],
) -> None:
    """Apply fn to each raw row inside its own savepoint; one bad record never stops the run."""
    for row in rows:
        ctx.conn.execute("SAVEPOINT rec")
        try:
            fn(row)
            db.mark_normalized(ctx.conn, row)
            ctx.conn.execute("RELEASE rec")
            ctx.stats[f"{label}_ok"] += 1
        except Incomplete as e:
            ctx.conn.execute("ROLLBACK TO rec")
            ctx.conn.execute("RELEASE rec")
            ctx.stats[f"{label}_incomplete"] += 1
            log.info("normalize %s %s deferred: %s", row["kind"], row["external_id"], e)
        except Exception as e:  # noqa: BLE001 - logged and counted
            ctx.conn.execute("ROLLBACK TO rec")
            ctx.conn.execute("RELEASE rec")
            ctx.stats[f"{label}_errors"] += 1
            log.error("normalize %s %s failed: %s", row["kind"], row["external_id"], e, exc_info=True)
    ctx.conn.commit()


def normalize_voteview(ctx: NormalizeContext, settings: Settings, all_records: bool) -> None:
    """The three Voteview files are parsed together whenever any of them changed."""
    ext = f"S{settings.congress.number}"
    rows = {k: db.get_raw(ctx.conn, "voteview", k, ext) for k in ("voteview_rollcalls", "voteview_votes", "voteview_members")}
    if any(r is None for r in rows.values()):
        return
    if not all_records and all(r["normalized_hash"] == r["payload_hash"] for r in rows.values()):
        return
    written = voteview.normalize_files(
        ctx, rows["voteview_rollcalls"]["payload"], rows["voteview_votes"]["payload"], rows["voteview_members"]["payload"]
    )
    for r in rows.values():
        db.mark_normalized(ctx.conn, r)
    ctx.conn.commit()
    ctx.stats["voteview_ok"] += written


def reclassify_house_resolutions(conn: sqlite3.Connection) -> int:
    """House votes on an H.Res. depend on whether it is a rule, which may be learned after the vote."""
    rows = conn.execute(
        "SELECT v.id, v.question, v.vote_class, v.threshold, b.bill_type, b.title FROM vote_event v"
        " JOIN bill b ON b.id = v.bill_id WHERE v.chamber='house' AND b.bill_type='hres'"
    ).fetchall()
    changed = 0
    for r in rows:
        cls = classify.classify("house", r["question"], is_rule=is_rule_title(r["bill_type"], r["title"]))
        thr = classify.threshold("house", cls)
        if cls != r["vote_class"] or thr != r["threshold"]:
            conn.execute("UPDATE vote_event SET vote_class=?, threshold=? WHERE id=?", (cls, thr, r["id"]))
            changed += 1
    conn.commit()
    return changed


def run_normalize(conn: sqlite3.Connection, settings: Settings, all_records: bool = False) -> Counter:
    ctx = NormalizeContext(
        conn=conn, crosswalk=load_crosswalk(conn, settings.congress.number), tracked=set(settings.members.tracked)
    )
    sync_tracked(ctx, settings.members.tracked)
    conn.commit()
    pending = lambda source, kind: db.pending_raw(conn, source, kind, all_records)  # noqa: E731

    _each(ctx, pending("congress_gov", "bill"), "bill", lambda r: bills.normalize_bill(ctx, r["payload"]))
    _each(
        ctx,
        pending("congress_gov", "bill_summaries"),
        "bill_summaries",
        lambda r: bills.normalize_summaries(ctx, r["payload"], r["external_id"]),
    )

    # House: a vote needs both its detail and member records; normalize when either changed.
    house_ids = {r["external_id"] for r in pending("congress_gov", "house_vote")}
    house_ids |= {r["external_id"] for r in pending("congress_gov", "house_vote_members")}

    def do_house(row: sqlite3.Row) -> None:
        members = db.get_raw(conn, "congress_gov", "house_vote_members", row["external_id"])
        if members is None:
            raise Incomplete("member positions not fetched yet")
        house.normalize_vote(ctx, row["payload"], members["payload"], row["url"])
        db.mark_normalized(conn, members)

    house_rows = [db.get_raw(conn, "congress_gov", "house_vote", e) for e in sorted(house_ids)]
    _each(ctx, [r for r in house_rows if r is not None], "house_vote", do_house)

    # Senate: only the configured source, so stored data from the other source never flip-flops.
    if settings.senate.source == "senate_gov":
        _each(
            ctx,
            pending("senate_xml", "senate_vote"),
            "senate_vote",
            lambda r: senate.normalize_vote(ctx, r["payload"], r["url"]),
        )
    else:
        normalize_voteview(ctx, settings, all_records)

    for kind, role in (("sponsored", "sponsor"), ("cosponsored", "cosponsor")):
        _each(
            ctx,
            pending("congress_gov", kind),
            kind,
            lambda r, role=role: bills.normalize_sponsorship_page(
                ctx, r["payload"], r["external_id"].split("/")[1], role, settings.congress.number
            ),
        )
    _each(
        ctx,
        pending("congress_gov", "bill_cosponsors"),
        "bill_cosponsors",
        lambda r: bills.normalize_bill_cosponsors(ctx, r["payload"], r["external_id"]),
    )

    ctx.stats["house_reclassified"] = reclassify_house_resolutions(conn)
    return ctx.stats

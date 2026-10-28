"""Normalize Voteview Senate CSVs into vote_event/member_vote, matching what senate.gov XML yields.

Differences from the XML, handled here:
- Only the date of each vote is published, not the time; votes are dated noon Eastern.
- There is no tie-breaker field. A tied vote that still carried can only have been decided by
  the Vice President voting yea, so that is recorded; a tied vote that failed records none.
- Nomination numbers lose their part suffix (PN11-7 becomes PN117); they are kept only to
  group votes on the same nomination, and the published description is shown instead.
- No amendment numbers; an amendment vote is linked to the bill it amends.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from .. import classify
from .common import get_or_create_bill, parse_bill_ref, upsert_legislator_minimal
from .context import NormalizeContext

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# Voteview cast codes: https://voteview.com/articles/data_help_votes
CAST = {
    "1": ("yea", "Yea"),  # yea
    "2": ("yea", "Yea"),  # paired yea
    "3": ("yea", "Yea"),  # announced yea
    "4": ("nay", "Nay"),  # announced nay
    "5": ("nay", "Nay"),  # paired nay
    "6": ("nay", "Nay"),  # nay
    "7": ("present", "Present"),
    "8": ("present", "Present"),
    "9": ("not_voting", "Not Voting"),
}  # "0" means not a member at the time of the vote: no row

PARTY = {"100": "D", "200": "R", "328": "I"}

FAILED = ("rejected", "defeated", "not sustained", "not well taken", "failed", "not agreed")
CARRIED = ("agreed to", "passed", "confirmed", "sustained", "well taken", "adopted")


def vote_date(day: str) -> str:
    """'2025-07-01' -> noon Eastern that day, in UTC."""
    local = datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(), time(12), EASTERN)
    return local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def inferred_tie_breaker(yea: int, nay: int, result: str) -> str | None:
    if yea != nay or yea == 0:
        return None
    r = result.lower()
    if any(w in r for w in FAILED):
        return None
    return "Yea" if any(w in r for w in CARRIED) else None


def display_name(bioname: str) -> str:
    """'TUBERVILLE, Thomas Hawley (Tommy)' -> 'Thomas Hawley Tuberville'."""
    last, _, first = bioname.partition(",")
    first = re.sub(r"\s*\(.*?\)", "", first).strip()
    return " ".join(p for p in (first, last.strip().title()) if p)


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def normalize_files(ctx: NormalizeContext, rollcalls: str, votes: str, members: str) -> int:
    """Upsert every Senate roll call in the files. Returns the number of roll calls written."""
    people = {m["icpsr"]: m for m in _rows(members)}
    positions: dict[str, list[dict[str, str]]] = defaultdict(list)
    for v in _rows(votes):
        if v["chamber"] == "Senate":
            positions[v["rollnumber"]].append(v)

    written = 0
    for rc in _rows(rollcalls):
        if rc["chamber"] != "Senate":
            continue
        ctx.conn.execute("SAVEPOINT vv")
        try:
            _write_rollcall(ctx, rc, positions.get(rc["rollnumber"], []), people)
            ctx.conn.execute("RELEASE vv")
            written += 1
            if written % 50 == 0:
                ctx.conn.commit()  # let other writers in during a long first load
        except Exception as e:  # noqa: BLE001 - one bad row never stops the rest
            ctx.conn.execute("ROLLBACK TO vv")
            ctx.conn.execute("RELEASE vv")
            ctx.stats["voteview_errors"] += 1
            log.error("voteview roll %s/%s: %s", rc.get("session"), rc.get("clerk_rollnumber"), e)
    return written


def _write_rollcall(ctx: NormalizeContext, rc: dict[str, str], votes: list[dict[str, str]], people: dict) -> None:
    congress = rc["congress"]
    session = f"{congress}-{rc['session']}"
    roll = int(rc["clerk_rollnumber"])
    date = vote_date(rc["date"])
    question = rc["vote_question"].strip()
    bill_number = (rc.get("bill_number") or "").strip()

    nomination_ref, bill_id = None, None
    if bill_number.upper().startswith("PN"):
        nomination_ref = bill_number.upper()
    elif bill_number:
        ref = parse_bill_ref(bill_number)
        if ref:
            bill_id = get_or_create_bill(ctx.conn, congress, *ref)

    vote_class = classify.classify("senate", question)
    thr = classify.threshold("senate", vote_class, rc.get("majority_requirement"))
    yea, nay = int(rc["yea_count"] or 0), int(rc["nay_count"] or 0)

    rows, tally = [], Counter()
    for v in votes:
        cast = CAST.get(v["cast_code"])
        if cast is None:
            continue  # 0: not a member at the time
        position, raw = cast
        member = people.get(v["icpsr"])
        if member and member["chamber"] != "Senate":
            continue  # the President's announced position, which Voteview records alongside senators
        bioguide = member["bioguide_id"] if member else ""
        if not bioguide:
            if v["icpsr"] not in ctx.unmapped_lis:
                log.warning("Voteview member %s has no Bioguide ID; skipping their rows", v["icpsr"])
            ctx.unmapped_lis[v["icpsr"]] += 1
            ctx.stats["voteview_unmapped"] += 1
            continue
        tally[position] += 1
        party = PARTY.get(member["party_code"], "I")
        person = ctx.crosswalk.by_bioguide.get(bioguide) if ctx.crosswalk else None
        if person:
            leg_id = ctx.legislator_from_person(person)
        else:
            leg_id = upsert_legislator_minimal(
                ctx.conn, bioguide, display_name(member["bioname"]), "senate", member["state_abbrev"]
            )
        caucus = ctx.crosswalk.caucus_on(bioguide, date, party) if ctx.crosswalk else party
        rows.append((leg_id, position, raw, party, caucus))

    vote_id = ctx.upsert_vote_event(
        chamber="senate",
        session=session,
        roll_number=roll,
        vote_date=date,
        question=question,
        vote_title=rc.get("vote_desc") or None,
        vote_class=vote_class,
        threshold=thr,
        result=rc.get("vote_result") or None,
        bill_id=bill_id,
        amendment_ref=None,
        nomination_ref=nomination_ref,
        tie_breaker=inferred_tie_breaker(yea, nay, rc.get("vote_result") or ""),
        yea=yea,
        nay=nay,
        present=tally["present"],
        not_voting=tally["not_voting"],
        source_url=f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{rc['session']}/"
        f"vote_{congress}_{rc['session']}_{roll:05d}.htm",
    )
    ctx.replace_member_votes(vote_id, [(vote_id, *r) for r in rows])

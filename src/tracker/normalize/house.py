"""Normalize Congress.gov House roll-call votes (detail + member positions) into vote_event/member_vote."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .. import classify
from .common import get_or_create_bill, is_rule_title, parse_bill_ref, upsert_legislator_minimal
from .context import NormalizeContext

log = logging.getLogger(__name__)

POSITIONS = {
    "yea": "yea",
    "aye": "yea",
    "nay": "nay",
    "no": "nay",
    "present": "present",
    "not voting": "not_voting",
}

AMENDMENT_DISPLAY = {"HAMDT": "H.Amdt.", "SAMDT": "S.Amdt.", "SUAMDT": "S.UP.Amdt."}


def to_utc(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(UTC).isoformat().replace("+00:00", "Z")


def map_positions(results: list[dict[str, Any]]) -> dict[str, str]:
    """voteCast -> position. Speaker elections record candidate names: a vote for the winner is
    'yea' and a vote for anyone else 'nay', so caucus positions stay meaningful."""
    casts = [r.get("voteCast", "") for r in results]
    unknown = [c for c in casts if c.strip().lower() not in POSITIONS]
    winner = Counter(unknown).most_common(1)[0][0] if unknown else None
    out = {}
    for c in set(casts):
        key = c.strip().lower()
        if key in POSITIONS:
            out[c] = POSITIONS[key]
        else:
            out[c] = "yea" if c == winner else "nay"
    return out


def amendment_id(vote: dict[str, Any]) -> str | None:
    """raw_record external_id of the amendment a vote is on, e.g. 'amendment/119/hamdt/85'."""
    if vote.get("amendmentType") and vote.get("amendmentNumber"):
        return f"amendment/{vote['congress']}/{vote['amendmentType'].lower()}/{vote['amendmentNumber']}"
    return None


def amended_bill_ref(amendment_payload: str) -> tuple[str, str, str] | None:
    amended = json.loads(amendment_payload).get("amendment", {}).get("amendedBill") or {}
    ref = parse_bill_ref(f"{amended.get('type', '')} {amended.get('number', '')}")
    return (str(amended.get("congress")), *ref) if ref else None


def direct_bill_ref(vote: dict[str, Any]) -> tuple[str, str, str] | None:
    """(congress, bill_type, number) linked on the vote itself; for amendment votes, the amended bill."""
    leg_type, leg_num = vote.get("legislationType"), vote.get("legislationNumber")
    if leg_type and leg_num:
        ref = parse_bill_ref(f"{leg_type} {leg_num}")
        if ref:
            return (str(vote["congress"]), *ref)
    return None


def resolve_bill(ctx: NormalizeContext, vote: dict[str, Any]) -> int | None:
    ref = direct_bill_ref(vote)
    if ref is None and (ext := amendment_id(vote)):
        # An amendment vote without a linked bill: use the amendment record fetched at ingest.
        raw = ctx.raw("congress_gov", "amendment", ext)
        ref = amended_bill_ref(raw["payload"]) if raw else None
    return get_or_create_bill(ctx.conn, *ref) if ref else None


def normalize_vote(ctx: NormalizeContext, detail_payload: str, members_payload: str, source_url: str | None) -> int:
    vote = json.loads(detail_payload)["houseRollCallVote"]
    members = json.loads(members_payload)["houseRollCallVoteMemberVotes"]["results"]
    congress = str(vote["congress"])
    session = f"{congress}-{vote['sessionNumber']}"
    roll = int(vote["rollCallNumber"])
    vote_date = to_utc(vote["startDate"])
    question = vote.get("voteQuestion") or ""

    bill_id = resolve_bill(ctx, vote)
    is_rule = False
    if bill_id is not None:
        b = ctx.conn.execute("SELECT bill_type, title FROM bill WHERE id=?", (bill_id,)).fetchone()
        is_rule = is_rule_title(b["bill_type"], b["title"])
    vote_class = classify.classify("house", question, is_rule=is_rule)
    thr = classify.threshold("house", vote_class)

    amendment_ref = None
    if vote.get("amendmentType") and vote.get("amendmentNumber"):
        amendment_ref = f"{AMENDMENT_DISPLAY.get(vote['amendmentType'], vote['amendmentType'])} {vote['amendmentNumber']}"

    pos_map = map_positions(members)
    tally = Counter(pos_map[m.get("voteCast", "")] for m in members)

    vote_id = ctx.upsert_vote_event(
        chamber="house",
        session=session,
        roll_number=roll,
        vote_date=vote_date,
        question=question,
        vote_title=vote.get("amendmentAuthor"),
        vote_class=vote_class,
        threshold=thr,
        result=vote.get("result"),
        bill_id=bill_id,
        amendment_ref=amendment_ref,
        nomination_ref=None,
        tie_breaker=None,
        yea=tally["yea"],
        nay=tally["nay"],
        present=tally["present"],
        not_voting=tally["not_voting"],
        source_url=vote.get("sourceDataURL") or source_url,
    )

    rows = []
    for m in members:
        bioguide = m.get("bioguideID")
        if not bioguide:
            ctx.stats["house_member_missing_id"] += 1
            continue
        party = m.get("voteParty") or "?"
        person = ctx.crosswalk.by_bioguide.get(bioguide) if ctx.crosswalk else None
        if person:
            leg_id = ctx.legislator_from_person(person)
        else:
            name = " ".join(p for p in (m.get("firstName"), m.get("lastName")) if p) or bioguide
            leg_id = upsert_legislator_minimal(ctx.conn, bioguide, name, "house", m.get("voteState"))
        caucus = ctx.crosswalk.caucus_on(bioguide, vote_date, party) if ctx.crosswalk else party
        cast = m.get("voteCast", "")
        rows.append((vote_id, leg_id, pos_map[cast], cast, party, caucus))
    ctx.replace_member_votes(vote_id, rows)
    return vote_id

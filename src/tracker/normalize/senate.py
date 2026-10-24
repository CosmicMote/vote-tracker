"""Normalize senate.gov roll-call vote XML into vote_event/member_vote."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import classify
from .common import find_bill_refs, get_or_create_bill, parse_bill_ref
from .context import NormalizeContext

log = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

POSITIONS = {
    "yea": "yea",
    "nay": "nay",
    "guilty": "yea",
    "not guilty": "nay",
    "present": "present",
    "present, giving live pair": "present",
    "not voting": "not_voting",
}


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return " ".join("".join(found.itertext()).split()) if found is not None else ""


def parse_vote_date(value: str) -> str:
    """'July 1, 2025,  11:56 AM' (Eastern) -> '2025-07-01T15:56:00Z'."""
    clean = " ".join(value.split())
    local = datetime.strptime(clean, "%B %d, %Y, %I:%M %p").replace(tzinfo=EASTERN)
    return local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def map_position(cast: str) -> str | None:
    return POSITIONS.get(cast.strip().lower())


def _int(s: str) -> int | None:
    return int(s) if s.strip().isdigit() else None


def nomination_ref_of(root: ET.Element) -> str | None:
    doc = root.find("document")
    if _text(doc, "document_type") != "PN":
        return None
    return _text(doc, "document_name") or f"PN{_text(doc, 'document_number')}"


def bill_ref_of(root: ET.Element) -> tuple[str, str, str] | None:
    """(congress, bill_type, number) of the bill a vote is on, or the bill an amendment amends."""
    if nomination_ref_of(root):
        return None
    doc = root.find("document")
    amd = root.find("amendment")
    doc_name = _text(doc, "document_name")
    ref = parse_bill_ref(doc_name) if doc_name else None
    if ref is None:
        # Amendment votes name the amendment as the document; link the bill it amends.
        for candidate in (_text(amd, "amendment_to_document_number"), _text(root, "vote_question_text")):
            refs = find_bill_refs(candidate)
            if refs:
                ref = refs[0]
                break
    if ref is None:
        return None
    return (_text(doc, "document_congress") or _text(root, "congress"), *ref)


def normalize_vote(ctx: NormalizeContext, payload: str, source_url: str | None) -> int:
    root = ET.fromstring(payload)
    congress = _text(root, "congress")
    session = f"{congress}-{_text(root, 'session')}"
    roll = int(_text(root, "vote_number"))
    vote_date = parse_vote_date(_text(root, "vote_date"))
    question = _text(root, "question")
    amendment_ref = _text(root.find("amendment"), "amendment_number") or None
    nomination_ref = nomination_ref_of(root)
    ref = bill_ref_of(root)
    bill_id = get_or_create_bill(ctx.conn, *ref) if ref else None

    vote_class = classify.classify("senate", question)
    thr = classify.threshold("senate", vote_class, _text(root, "majority_requirement"))
    tie = _text(root, "tie_breaker/tie_breaker_vote") or None

    rows = []
    tally: Counter = Counter()
    for m in root.iter("member"):
        lis = _text(m, "lis_member_id")
        cast = _text(m, "vote_cast")
        position = map_position(cast)
        if position is None:
            log.warning("senate %s roll %d: unknown vote_cast %r for %s", session, roll, cast, lis)
            ctx.stats["senate_unknown_vote_cast"] += 1
            continue
        tally[position] += 1
        person = ctx.crosswalk.from_lis(lis) if ctx.crosswalk else None
        if person is None:
            if lis not in ctx.unmapped_lis:
                log.warning("LIS ID %s (%s) not in crosswalk; skipping their member rows", lis, _text(m, "member_full"))
            ctx.unmapped_lis[lis] += 1
            ctx.stats["senate_unmapped_lis"] += 1
            continue
        party = _text(m, "party") or person.party
        leg_id = ctx.legislator_from_person(person)
        caucus = ctx.crosswalk.caucus_on(person.bioguide, vote_date, party)
        rows.append((None, leg_id, position, cast, party, caucus))

    count = root.find("count")
    yea = _int(_text(count, "yeas"))
    nay = _int(_text(count, "nays"))
    present = _int(_text(count, "present"))
    absent = _int(_text(count, "absent"))

    vote_id = ctx.upsert_vote_event(
        chamber="senate",
        session=session,
        roll_number=roll,
        vote_date=vote_date,
        question=question,
        vote_title=_text(root, "vote_title") or None,
        vote_class=vote_class,
        threshold=thr,
        result=_text(root, "vote_result") or None,
        bill_id=bill_id,
        amendment_ref=amendment_ref,
        nomination_ref=nomination_ref,
        tie_breaker=tie,
        # Published counts first; member tallies when a count is blank.
        yea=yea if yea is not None else tally["yea"],
        nay=nay if nay is not None else tally["nay"],
        present=present if present is not None else tally["present"],
        not_voting=absent if absent is not None else tally["not_voting"],
        source_url=source_url,
    )
    ctx.replace_member_votes(vote_id, [(vote_id, *r[1:]) for r in rows])
    return vote_id


_MENU_NUM = re.compile(r"\d+")


def parse_menu(payload: str) -> list[dict[str, str]]:
    """Vote menu entries: roll number, date ('18-Dec'), question, result."""
    root = ET.fromstring(payload)
    year = _text(root, "congress_year")
    out = []
    for v in root.iter("vote"):
        num = _text(v, "vote_number")
        if not _MENU_NUM.fullmatch(num):
            continue
        q = v.find("question")
        out.append(
            {
                "roll": str(int(num)),
                "date": f"{_text(v, 'vote_date')}-{year}",
                "question": (q.text or "").strip() if q is not None else "",
                "issue": _text(v, "issue"),
                "result": _text(v, "result"),
            }
        )
    return out


def menu_date_iso(value: str) -> str:
    """'18-Dec-2025' -> '2025-12-18'."""
    return datetime.strptime(value, "%d-%b-%Y").date().isoformat()

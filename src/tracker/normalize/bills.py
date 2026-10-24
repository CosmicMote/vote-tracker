"""Normalize Congress.gov bills, CRS summaries, sponsorships and rule links."""

from __future__ import annotations

import json
import logging
from typing import Any

from .common import (
    bill_web_url,
    get_or_create_bill,
    is_rule_title,
    parse_bill_ref,
    rule_targets,
    strip_html,
)
from .context import NormalizeContext

log = logging.getLogger(__name__)


def normalize_bill(ctx: NormalizeContext, payload: str) -> int | None:
    bill = json.loads(payload).get("bill") or {}
    ref = parse_bill_ref(f"{bill.get('type', '')} {bill.get('number', '')}")
    if not ref:
        return None
    congress = str(bill.get("congress"))
    bill_id = get_or_create_bill(ctx.conn, congress, *ref)
    title = bill.get("title")
    ctx.conn.execute(
        "UPDATE bill SET title=?, introduced=?, policy_area=?, url=?, updated_at=? WHERE id=?",
        (
            title,
            bill.get("introducedDate"),
            (bill.get("policyArea") or {}).get("name"),
            bill_web_url(congress, *ref),
            bill.get("updateDateIncludingText") or bill.get("updateDate"),
            bill_id,
        ),
    )
    if is_rule_title(ref[0], title):
        ctx.conn.execute("DELETE FROM rule_link WHERE rule_bill_id=?", (bill_id,))
        for target in rule_targets(ref[1], title):
            target_id = get_or_create_bill(ctx.conn, congress, *target)
            ctx.conn.execute(
                "INSERT OR IGNORE INTO rule_link (rule_bill_id, underlying_bill_id) VALUES (?, ?)",
                (bill_id, target_id),
            )
    return bill_id


def normalize_summaries(ctx: NormalizeContext, payload: str, external_id: str) -> int | None:
    # external_id: 'bill/119/hr/3424'
    _, congress, bill_type, number = external_id.split("/")
    bill_id = get_or_create_bill(ctx.conn, congress, bill_type, number)
    for s in json.loads(payload).get("summaries") or []:
        text = strip_html(s.get("text"))
        if not text:
            continue
        ctx.conn.execute(
            "INSERT INTO bill_summary (bill_id, source, version_code, action_date, text)"
            " VALUES (?, 'crs', ?, ?, ?)"
            " ON CONFLICT(bill_id, source, version_code) DO UPDATE SET action_date=excluded.action_date, text=excluded.text",
            (bill_id, str(s.get("versionCode", "")), s.get("actionDate"), text),
        )
    return bill_id


def _item_ref(item: dict[str, Any]) -> tuple[str, str] | None:
    if not item.get("type") or not item.get("number"):
        return None  # amendments appear in these lists without a bill type
    return parse_bill_ref(f"{item['type']} {item['number']}")


def normalize_sponsorship_page(ctx: NormalizeContext, payload: str, bioguide: str, role: str, congress: int) -> int:
    """One page of /member/{id}/sponsored-legislation or /cosponsored-legislation."""
    row = ctx.conn.execute("SELECT id FROM legislator WHERE bioguide_id=?", (bioguide,)).fetchone()
    if row is None:
        log.warning("sponsorships for unknown legislator %s skipped", bioguide)
        return 0
    key = "sponsoredLegislation" if role == "sponsor" else "cosponsoredLegislation"
    n = 0
    for item in json.loads(payload).get(key) or []:
        if str(item.get("congress")) != str(congress):
            continue
        ref = _item_ref(item)
        if not ref:
            continue
        bill_id = get_or_create_bill(ctx.conn, str(congress), *ref)
        # Fill a stub bill from the list item when the full bill has not been fetched yet.
        ctx.conn.execute(
            "UPDATE bill SET title=COALESCE(title, ?), introduced=COALESCE(introduced, ?),"
            " policy_area=COALESCE(policy_area, ?) WHERE id=?",
            (item.get("title"), item.get("introducedDate"), (item.get("policyArea") or {}).get("name"), bill_id),
        )
        if role == "sponsor":
            date = item.get("introducedDate")
        else:
            date = item.get("cosponsoredDate") or item.get("sponsorshipDate")
        withdrawn = item.get("sponsorshipWithdrawnDate")
        ctx.conn.execute(
            "INSERT INTO sponsorship (bill_id, legislator_id, role, date, withdrawn) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(bill_id, legislator_id, role) DO UPDATE SET"
            " date=COALESCE(excluded.date, sponsorship.date),"
            " withdrawn=COALESCE(excluded.withdrawn, sponsorship.withdrawn)",
            (bill_id, row["id"], role, date, withdrawn),
        )
        n += 1
    return n


def normalize_bill_cosponsors(ctx: NormalizeContext, payload: str, external_id: str) -> int:
    """/bill/{c}/{t}/{n}/cosponsors gives each cosponsor's sponsorshipDate; only tracked members matter."""
    _, congress, bill_type, number = external_id.split("/")[:4]
    bill_id = get_or_create_bill(ctx.conn, congress, bill_type, number)
    n = 0
    for c in json.loads(payload).get("cosponsors") or []:
        bioguide = c.get("bioguideId")
        if bioguide not in ctx.tracked:
            continue
        row = ctx.conn.execute("SELECT id FROM legislator WHERE bioguide_id=?", (bioguide,)).fetchone()
        if row is None:
            continue
        ctx.conn.execute(
            "INSERT INTO sponsorship (bill_id, legislator_id, role, date, withdrawn) VALUES (?, ?, 'cosponsor', ?, ?)"
            " ON CONFLICT(bill_id, legislator_id, role) DO UPDATE SET date=excluded.date, withdrawn=excluded.withdrawn",
            (bill_id, row["id"], c.get("sponsorshipDate"), c.get("sponsorshipWithdrawnDate")),
        )
        n += 1
    return n

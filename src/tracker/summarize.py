"""Stage 4: plain-language bill summaries for bills that made the digest.

The model only ever sees bill material, never how anyone voted. Nominations are never sent.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from . import db
from .analyze import Period, underlying_bills
from .config import Settings
from .llm.base import ProviderError, SummaryProvider
from .llm.prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT, BillSummaryOut, user_message
from .normalize.common import bill_display, strip_html
from .sources.congress_gov import CongressGovClient
from .sources.http import HttpError

log = logging.getLogger(__name__)

BasisKind = Literal["crs", "bill_text", "title_only"]

PROVENANCE = {
    "crs": "Summary based on the Congressional Research Service summary",
    "bill_text": "AI summary of the bill text; no official summary yet",
    "title_only": "No summary or text published yet",
    "crs_excerpt": "Excerpt from the Congressional Research Service summary; AI summary unavailable",
    "unavailable": "Bill text published, but no summary is available",
}


@dataclass(frozen=True)
class Basis:
    kind: BasisKind
    version: str
    text: str
    truncated: bool = False


def bill_row(conn: sqlite3.Connection, bill_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM bill WHERE id=?", (bill_id,)).fetchone()


def latest_crs(conn: sqlite3.Connection, bill_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bill_summary WHERE bill_id=? AND source='crs'"
        " ORDER BY COALESCE(action_date, '') DESC, version_code DESC LIMIT 1",
        (bill_id,),
    ).fetchone()


def _text_ext(b: sqlite3.Row) -> str:
    return f"bill/{b['session']}/{b['bill_type']}/{b['number']}"


def latest_text(conn: sqlite3.Connection, b: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM raw_record WHERE source='congress_gov' AND kind='bill_text_version' AND external_id LIKE ?"
        " ORDER BY external_id DESC LIMIT 1",
        (_text_ext(b) + "/%",),
    ).fetchone()


_SECTION_RE = re.compile(r"\n(?=(?:SEC\.|SECTION|Sec\.)\s+\d+)")


def truncate_at_section(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut to max_chars, at the last section heading (or paragraph) before the limit when possible."""
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    cuts = [m.start() for m in _SECTION_RE.finditer(head)]
    cut = cuts[-1] if cuts and cuts[-1] > max_chars // 2 else head.rfind("\n\n")
    if cut <= max_chars // 2:
        cut = max_chars
    return head[:cut].rstrip(), True


def choose_basis(conn: sqlite3.Connection, bill_id: int, max_input_chars: int) -> Basis:
    """Offline: the latest CRS summary, else the latest stored bill text, else the title only."""
    crs = latest_crs(conn, bill_id)
    if crs:
        return Basis("crs", crs["version_code"], crs["text"])
    b = bill_row(conn, bill_id)
    raw = latest_text(conn, b)
    if raw:
        text, truncated = truncate_at_section(strip_html(raw["payload"]), max_input_chars)
        if text.strip():
            return Basis("bill_text", raw["external_id"].rsplit("/", 1)[1], text, truncated)
    return Basis("title_only", "title", b["title"] or "")


def ensure_bill_text(conn: sqlite3.Connection, cg: CongressGovClient, bill_id: int) -> bool:
    """Fetch the latest formatted text version, only for bills without a CRS summary."""
    b = bill_row(conn, bill_id)
    if latest_crs(conn, bill_id) or latest_text(conn, b):
        return False
    ext = _text_ext(b)
    listing = cg.bill_text(b["session"], b["bill_type"], b["number"])
    db.store_raw(conn, "congress_gov", "bill_text", ext, listing.text, listing.url)
    versions = [v for v in listing.json().get("textVersions") or [] if v.get("formats")]
    versions.sort(key=lambda v: v.get("date") or "", reverse=True)
    for v in versions:
        fmt = next((f for f in v["formats"] if f.get("type") == "Formatted Text"), None)
        if fmt and fmt.get("url"):
            doc = cg.fetch_url(fmt["url"])
            version = (v.get("date") or "undated")[:10]
            db.store_raw(conn, "congress_gov", "bill_text_version", f"{ext}/{version}", doc.text, doc.url)
            conn.commit()
            return True
    conn.commit()
    return False


def summary_model(settings: Settings) -> str:
    """The model that writes summaries, as '<provider>:<model>' (matches SummaryProvider.name)."""
    return f"{settings.llm.provider}:{settings.llm.model}"


def cached_summary(conn: sqlite3.Connection, bill_id: int, basis: Basis, settings: Settings) -> sqlite3.Row | None:
    """The summary for this basis written with the configured prompt version and model, if any."""
    return conn.execute(
        "SELECT * FROM llm_summary WHERE bill_id=? AND basis=? AND basis_version=? AND prompt_version=? AND model=?",
        (bill_id, basis.kind, basis.version, settings.llm.prompt_version, summary_model(settings)),
    ).fetchone()


def summarize_bill(
    conn: sqlite3.Connection, provider: SummaryProvider, bill_id: int, settings: Settings
) -> str:
    """Ensure an llm_summary exists for the bill's current basis. Returns what happened."""
    basis = choose_basis(conn, bill_id, settings.llm.max_input_chars)
    if basis.kind == "title_only":
        return "title_only"
    if cached_summary(conn, bill_id, basis, settings):
        return "cached"
    b = bill_row(conn, bill_id)
    text = basis.text + ("\n\n[Material truncated.]" if basis.truncated else "")
    user = user_message(
        bill_display(b["bill_type"], b["number"]), b["title"], b["short_title"], b["policy_area"], basis.kind, text
    )
    attempt_user = user
    for attempt in (1, 2):
        try:
            raw = provider.complete_json(SYSTEM_PROMPT, attempt_user, OUTPUT_SCHEMA)
        except ProviderError as e:
            log.error("summary for %s failed: %s", bill_display(b["bill_type"], b["number"]), e)
            return "failed"
        try:
            parsed = BillSummaryOut.model_validate_json(raw)
        except ValidationError as e:
            log.warning("summary for bill %d failed validation (attempt %d): %s", bill_id, attempt, e)
            attempt_user = (
                f"{user}\n\nYour previous response was rejected:\n{e}\n"
                "Return corrected JSON that matches the schema exactly."
            )
            continue
        conn.execute(
            "INSERT INTO llm_summary (bill_id, basis, basis_version, prompt_version, model, output_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                bill_id,
                basis.kind,
                basis.version,
                settings.llm.prompt_version,
                summary_model(settings),
                parsed.model_dump_json(),
                db.utcnow(),
            ),
        )
        conn.commit()
        return "created"
    log.error("summary for bill %d invalid twice; the digest will use the fallback", bill_id)
    return "failed"


def digest_bill_ids(conn: sqlite3.Connection, period: Period) -> list[int]:
    """Bills attached to selected findings, in a stable order. Rules map to their underlying bills."""
    rows = conn.execute(
        "SELECT f.bill_id, f.vote_event_id FROM finding f"
        " WHERE f.selected=1 AND f.period_start=? AND f.period_end=? AND f.bill_id IS NOT NULL"
        " ORDER BY f.legislator_id, f.id",
        (period.start.isoformat(), period.end.isoformat()),
    ).fetchall()
    out: list[int] = []
    for r in rows:
        for bid in underlying_bills(conn, r["bill_id"]):
            if bid not in out:
                out.append(bid)
    return out


def run_summarize(
    conn: sqlite3.Connection,
    settings: Settings,
    period: Period,
    provider: SummaryProvider,
    cg: CongressGovClient | None = None,
) -> dict[str, int]:
    stats: dict[str, int] = {}
    for bill_id in digest_bill_ids(conn, period):
        if cg is not None:
            try:
                ensure_bill_text(conn, cg, bill_id)
            except (HttpError, ValueError) as e:
                log.warning("bill text for bill %d not fetched: %s", bill_id, e)
        outcome = summarize_bill(conn, provider, bill_id, settings)
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


# --- read side, for render ------------------------------------------------------


def first_sentences(text: str, n: int = 2) -> str:
    # Skip a leading title line (CRS summaries often start with the bill's short title).
    paras = [p for p in text.split("\n") if p.strip()]
    body = " ".join(paras[1:]) if len(paras) > 1 else " ".join(paras)
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", body.strip())
    return " ".join(sentences[:n]).strip()


def summary_view(conn: sqlite3.Connection, bill_id: int, settings: Settings) -> dict:
    """What the digest shows for a bill: LLM bullets, or a deterministic fallback."""
    b = bill_row(conn, bill_id)
    basis = choose_basis(conn, bill_id, settings.llm.max_input_chars)
    view = {
        "bill_id": bill_id,
        "number": bill_display(b["bill_type"], b["number"]),
        "official_title": b["title"],
        "url": b["url"],
        "plain_title": b["title"] or bill_display(b["bill_type"], b["number"]),
        "bullets": [],
        "caveat": None,
        "excerpt": None,
    }
    if basis.kind == "title_only":
        return {**view, "provenance": PROVENANCE["title_only"]}
    cached = cached_summary(conn, bill_id, basis, settings)
    if cached:
        out = json.loads(cached["output_json"])
        return {
            **view,
            "plain_title": out["plain_title"],
            "bullets": out["what_it_does"],
            "caveat": out.get("caveat"),
            "provenance": PROVENANCE[basis.kind],
        }
    if basis.kind == "crs":
        return {**view, "excerpt": first_sentences(basis.text), "provenance": PROVENANCE["crs_excerpt"]}
    return {**view, "provenance": PROVENANCE["unavailable"]}


def pending_summaries(conn: sqlite3.Connection, period: Period, settings: Settings) -> list[int]:
    """Bills in the week's digest that could get an AI summary but do not have one yet."""
    pending = []
    for bill_id in digest_bill_ids(conn, period):
        basis = choose_basis(conn, bill_id, settings.llm.max_input_chars)
        if basis.kind != "title_only" and not cached_summary(conn, bill_id, basis, settings):
            pending.append(bill_id)
    return pending

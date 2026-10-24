"""Stage 1: fetch new and changed records into raw_record.

House and Senate are independent: a failure in one never blocks the other. A failure on a
single record is logged and skipped, and the run finishes as 'partial'.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from . import db
from .config import Settings
from .normalize import house as house_norm
from .normalize import load_crosswalk
from .normalize import senate as senate_norm
from .normalize.common import is_rule_title, parse_bill_ref, rule_targets
from .sources.congress_gov import CongressGovClient
from .sources.http import HttpClient, HttpError
from .sources.legislators import Crosswalk, Person, fetch_crosswalk
from .sources.senate_xml import SenateClient
from .sources.voteview import VoteviewClient

log = logging.getLogger(__name__)

BillRef = tuple[str, str, str]  # (congress, bill_type, number)


@dataclass
class Sources:
    http: HttpClient
    congress: CongressGovClient | None
    senate: SenateClient
    voteview: VoteviewClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "Sources":
        http = HttpClient(
            settings.http.user_agent,
            settings.http.min_interval_seconds,
            settings.http.timeout_seconds,
            settings.http.max_attempts,
        )
        key = settings.secrets.congress_gov_api_key
        return cls(
            http=http,
            congress=CongressGovClient(http, key) if key else None,
            senate=SenateClient(http),
            voteview=VoteviewClient(http, settings.senate.voteview_url),
        )


@dataclass
class IngestResult:
    stats: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    failed_sources: list[str] = field(default_factory=list)
    # Bills to fetch: ref -> True when referenced by something recent (a fresh CRS summary may exist).
    bill_refs: dict[BillRef, bool] = field(default_factory=dict)

    def want_bill(self, ref: BillRef | None, recent: bool) -> None:
        if ref:
            self.bill_refs[ref] = self.bill_refs.get(ref, False) or recent

    @property
    def errors(self) -> int:
        return sum(c["errors"] for c in self.stats.values())

    @property
    def status(self) -> str:
        if self.failed_sources or self.errors:
            return "partial"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "sources": {k: dict(v) for k, v in sorted(self.stats.items())},
            "failed_sources": self.failed_sources,
        }


def select_targets(
    items: list[tuple[int, str]],
    cursor: int,
    since: str | None,
    cutoff: str,
    stored: set[int],
) -> list[int]:
    """Roll numbers to fetch from a vote list of (roll, ISO date).

    New votes are those above the cursor. With no cursor yet, start from --since or, by default,
    the corrections window. --since also backfills anything on or after it not yet stored.
    Every vote inside the corrections window is re-fetched.
    """
    out = set()
    for roll, day in items:
        if cursor > 0:
            new = roll > cursor
        else:
            new = day >= (since or cutoff)
        backfill = since is not None and day >= since and roll not in stored
        recent = day >= cutoff
        if new or backfill or recent:
            out.add(roll)
    return sorted(out)


def next_cursor(old: int, fetched: list[int], failed: list[int]) -> int:
    """Advance past fetched votes, but never past a failure so it is retried next run."""
    if failed:
        return max(old, min(failed) - 1)
    return max([old, *fetched])


def _stored_rolls(conn: sqlite3.Connection, source: str, kind: str, prefix: str) -> set[int]:
    rows = conn.execute(
        "SELECT external_id FROM raw_record WHERE source=? AND kind=? AND external_id LIKE ?",
        (source, kind, prefix + "%"),
    ).fetchall()
    return {int(r["external_id"].rsplit("/", 1)[1]) for r in rows}


def _age_days(fetched_at: str, now: datetime) -> float:
    then = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    return (now - then).total_seconds() / 86400


# --- crosswalk & members ------------------------------------------------------


def ensure_crosswalk(
    conn: sqlite3.Connection, settings: Settings, http: HttpClient, result: IngestResult, now: datetime, force: bool = False
) -> Crosswalk:
    """Refresh the congress-legislators files when older than the configured age."""
    files = [("legislators_current", "current", settings.storage.crosswalk_url)]
    if settings.storage.crosswalk_historical_url:
        files.append(("legislators_historical", "historical", settings.storage.crosswalk_historical_url))
    stats = result.stats["legislators"]
    for kind, ext, url in files:
        row = db.get_raw(conn, "legislators", kind, ext)
        if row and not force and _age_days(row["fetched_at"], now) < settings.storage.crosswalk_max_age_days:
            stats["fresh"] += 1
            continue
        try:
            fetched = fetch_crosswalk(http, url)
            json.loads(fetched.text)  # refuse to store something that is not JSON
            stats["changed" if db.store_raw(conn, "legislators", kind, ext, fetched.text, fetched.url) else "unchanged"] += 1
            conn.commit()
        except Exception as e:  # noqa: BLE001
            if row is None and kind == "legislators_current":
                raise
            log.warning("could not refresh %s (%s); using the copy from %s", kind, e, row["fetched_at"] if row else "nowhere")
            stats["errors"] += 1
    db.set_sync(conn, "legislators", None)
    conn.commit()
    crosswalk = load_crosswalk(conn, settings.congress.number)
    assert crosswalk is not None
    return crosswalk


def resolve_tracked(crosswalk: Crosswalk, tracked: list[str]) -> list[Person]:
    """Fail fast if any configured Bioguide ID cannot be resolved."""
    return [crosswalk.resolve(b) for b in tracked]


def ingest_members(
    conn: sqlite3.Connection,
    cg: CongressGovClient,
    people: list[Person],
    congress: int,
    result: IngestResult,
    cutoff: str,
) -> None:
    stats = result.stats["congress_gov:members"]
    for person in people:
        b = person.bioguide
        cosponsored: list[BillRef] = []
        try:
            f = cg.member(b)
            db.store_raw(conn, "congress_gov", "member", f"member/{b}", f.text, f.url)
            stats["fetched"] += 1
        except HttpError as e:
            log.error("member %s: %s", b, e)
            stats["errors"] += 1
        for kind, pages, key in (
            ("sponsored", cg.sponsored, "sponsoredLegislation"),
            ("cosponsored", cg.cosponsored, "cosponsoredLegislation"),
        ):
            try:
                for offset, page in pages(b):
                    db.store_raw(conn, "congress_gov", kind, f"member/{b}/{kind}/{offset}", page.text, page.url)
                    stats[f"{kind}_pages"] += 1
                    for item in page.json().get(key) or []:
                        if str(item.get("congress")) != str(congress) or not item.get("type"):
                            continue  # other Congresses, and amendments (no bill type)
                        ref = parse_bill_ref(f"{item['type']} {item.get('number', '')}")
                        if not ref:
                            continue
                        recent = (item.get("introducedDate") or "") >= cutoff
                        result.want_bill((str(congress), *ref), recent)
                        if kind == "cosponsored" and not item.get("cosponsoredDate"):
                            cosponsored.append((str(congress), *ref))
            except HttpError as e:
                log.error("%s legislation for %s: %s", kind, b, e)
                stats["errors"] += 1
        conn.commit()
        ingest_cosponsor_dates(conn, cg, b, cosponsored, stats)


def ingest_cosponsor_dates(
    conn: sqlite3.Connection, cg: CongressGovClient, bioguide: str, refs: list[BillRef], stats: Counter
) -> None:
    """The member's cosponsored list carries no cosponsorship date; the bill's cosponsor list does.
    Fetch it once per bill, when the stored copy does not yet include this member."""
    for congress, bill_type, number in refs:
        prefix = f"bill/{congress}/{bill_type}/{number}/"
        known = conn.execute(
            "SELECT 1 FROM raw_record WHERE source='congress_gov' AND kind='bill_cosponsors'"
            " AND external_id LIKE ? AND instr(payload, ?) > 0 LIMIT 1",
            (prefix + "%", f'"{bioguide}"'),
        ).fetchone()
        if known:
            continue
        try:
            for offset, page in cg.bill_cosponsors(congress, bill_type, number):
                db.store_raw(conn, "congress_gov", "bill_cosponsors", f"{prefix}{offset}", page.text, page.url)
            conn.commit()
            stats["cosponsor_lists"] += 1
        except HttpError as e:
            conn.rollback()
            log.error("cosponsors of %s: %s", prefix.rstrip("/"), e)
            stats["errors"] += 1


# --- House ------------------------------------------------------------------


def ingest_house(
    conn: sqlite3.Connection,
    cg: CongressGovClient,
    settings: Settings,
    result: IngestResult,
    since: str | None,
    cutoff: str,
) -> None:
    congress = settings.congress.number
    stats = result.stats["congress_gov:house"]
    for session_id in settings.sessions:
        session = int(session_id.split("-")[1])
        sync_key = f"house_votes:{session_id}"
        items: list[tuple[int, str]] = []
        try:
            for offset, page in cg.house_votes(congress, session):
                db.store_raw(conn, "congress_gov", "house_vote_list", f"house/{congress}/{session}/offset/{offset}", page.text, page.url)
                for v in page.json().get("houseRollCallVotes") or []:
                    items.append((int(v["rollCallNumber"]), house_norm.to_utc(v["startDate"])[:10]))
        except HttpError as e:
            if e.status == 404:
                log.info("no House votes listed for %s", session_id)
                continue
            raise
        conn.commit()
        sync = db.get_sync(conn, sync_key)
        cursor = int(sync["cursor"]) if sync and sync["cursor"] else 0
        stored = _stored_rolls(conn, "congress_gov", "house_vote_members", f"house/{congress}/{session}/")
        targets = select_targets(items, cursor, since, cutoff, stored)
        dates = dict(items)
        fetched, failed = [], []
        for roll in targets:
            ext = f"house/{congress}/{session}/{roll}"
            try:
                detail = cg.house_vote(congress, session, roll)
                members = cg.house_vote_members(congress, session, roll)
                changed = db.store_raw(conn, "congress_gov", "house_vote", ext, detail.text, detail.url)
                changed |= db.store_raw(conn, "congress_gov", "house_vote_members", ext, members.text, members.url)
                vote = detail.json()["houseRollCallVote"]
                ref = house_norm.direct_bill_ref(vote)
                if ref is None and (amd := house_norm.amendment_id(vote)):
                    _, c, t, n = amd.split("/")
                    a = cg.amendment(c, t, n)
                    db.store_raw(conn, "congress_gov", "amendment", amd, a.text, a.url)
                    ref = house_norm.amended_bill_ref(a.text)
                result.want_bill(ref, dates[roll] >= cutoff)
                conn.commit()
                stats["changed" if changed else "unchanged"] += 1
                fetched.append(roll)
            except (HttpError, KeyError, ValueError) as e:
                conn.rollback()
                log.error("house vote %s: %s", ext, e)
                stats["errors"] += 1
                failed.append(roll)
        db.set_sync(conn, sync_key, str(next_cursor(cursor, fetched, failed)))
        conn.commit()


# --- Senate -----------------------------------------------------------------


def ingest_senate(
    conn: sqlite3.Connection,
    senate: SenateClient,
    settings: Settings,
    result: IngestResult,
    since: str | None,
    cutoff: str,
) -> None:
    congress = settings.congress.number
    stats = result.stats["senate_xml"]
    for session_id in settings.sessions:
        session = int(session_id.split("-")[1])
        sync_key = f"senate_votes:{session_id}"
        try:
            menu = senate.vote_menu(congress, session)
        except HttpError as e:
            if e.status == 404:
                log.info("no Senate vote menu for %s", session_id)
                continue
            raise
        db.store_raw(conn, "senate_xml", "senate_vote_menu", f"senate/{congress}/{session}", menu.text, menu.url)
        conn.commit()
        items = [(int(v["roll"]), senate_norm.menu_date_iso(v["date"])) for v in senate_norm.parse_menu(menu.text)]
        sync = db.get_sync(conn, sync_key)
        cursor = int(sync["cursor"]) if sync and sync["cursor"] else 0
        stored = _stored_rolls(conn, "senate_xml", "senate_vote", f"senate/{congress}/{session}/")
        targets = select_targets(items, cursor, since, cutoff, stored)
        dates = dict(items)
        fetched, failed = [], []
        for roll in targets:
            ext = f"senate/{congress}/{session}/{roll}"
            try:
                f = senate.vote(congress, session, roll)
                root = ET.fromstring(f.text)  # refuse to store a non-XML (e.g. block) page
                changed = db.store_raw(conn, "senate_xml", "senate_vote", ext, f.text, f.url)
                result.want_bill(senate_norm.bill_ref_of(root), dates[roll] >= cutoff)
                conn.commit()
                stats["changed" if changed else "unchanged"] += 1
                fetched.append(roll)
            except (HttpError, ET.ParseError) as e:
                conn.rollback()
                log.error("senate vote %s: %s", ext, e)
                stats["errors"] += 1
                failed.append(roll)
        db.set_sync(conn, sync_key, str(next_cursor(cursor, fetched, failed)))
        conn.commit()


def ingest_senate_voteview(
    conn: sqlite3.Connection,
    vv: VoteviewClient,
    settings: Settings,
    result: IngestResult,
    since: str | None,
    cutoff: str,
) -> None:
    """Download the Congress's three Senate files whole; corrections arrive with every download.
    Bills are queued for the same votes the senate.gov path would fetch: new, recent, or --since."""
    congress = settings.congress.number
    stats = result.stats["voteview"]
    ext = f"S{congress}"
    for kind in ("voteview_rollcalls", "voteview_votes", "voteview_members"):
        f = vv.fetch(kind, congress)
        if not f.text.lstrip().startswith("congress,"):
            raise ValueError(f"{f.url} did not return a Voteview CSV")
        stats["changed" if db.store_raw(conn, "voteview", kind, ext, f.text, f.url) else "unchanged"] += 1
    conn.commit()

    rollcalls = [r for r in csv.DictReader(io.StringIO(db.get_raw(conn, "voteview", "voteview_rollcalls", ext)["payload"]))
                 if r["chamber"] == "Senate"]
    for session_id in settings.sessions:
        session = session_id.split("-")[1]
        rows = {int(r["clerk_rollnumber"]): r for r in rollcalls if r["session"] == session}
        if not rows:
            continue
        sync_key = f"voteview_votes:{session_id}"
        sync = db.get_sync(conn, sync_key)
        cursor = int(sync["cursor"]) if sync and sync["cursor"] else 0
        items = [(roll, r["date"]) for roll, r in rows.items()]
        targets = select_targets(items, cursor, since, cutoff, set(range(1, cursor + 1)))
        for roll in targets:
            ref = parse_bill_ref(rows[roll].get("bill_number") or "")
            result.want_bill((str(congress), *ref) if ref else None, rows[roll]["date"] >= cutoff)
        stats["new_votes"] += sum(1 for roll in rows if roll > cursor)
        db.set_sync(conn, sync_key, str(max(rows)))
    conn.commit()


# --- Bills ------------------------------------------------------------------


def ingest_bills(
    conn: sqlite3.Connection, cg: CongressGovClient, settings: Settings, result: IngestResult, now: datetime
) -> None:
    """Fetch each referenced bill and its CRS summaries: new ones, recently referenced ones, and stale ones."""
    stats = result.stats["congress_gov:bills"]
    queue = sorted(result.bill_refs.items())
    seen: set[BillRef] = set()
    while queue:
        (congress, bill_type, number), recent = queue.pop(0)
        if (congress, bill_type, number) in seen:
            continue
        seen.add((congress, bill_type, number))
        ext = f"bill/{congress}/{bill_type}/{number}"
        existing = db.get_raw(conn, "congress_gov", "bill", ext)
        stale = existing is not None and _age_days(existing["fetched_at"], now) >= settings.storage.crosswalk_max_age_days
        if existing is not None and not recent and not stale:
            stats["skipped"] += 1
            continue
        try:
            b = cg.bill(congress, bill_type, number)
            s = cg.bill_summaries(congress, bill_type, number)
            changed = db.store_raw(conn, "congress_gov", "bill", ext, b.text, b.url)
            changed |= db.store_raw(conn, "congress_gov", "bill_summaries", ext, s.text, s.url)
            conn.commit()
            stats["changed" if changed else "unchanged"] += 1
            title = b.json().get("bill", {}).get("title")
            if is_rule_title(bill_type, title):
                for t in rule_targets(number, title):
                    queue.append(((congress, *t), recent))
        except (HttpError, ValueError) as e:
            conn.rollback()
            log.error("bill %s: %s", ext, e)
            stats["errors"] += 1


# --- entry point --------------------------------------------------------------


def record_chamber_health(conn: sqlite3.Connection, chamber: str, ok: bool) -> None:
    """sync_state rows the digest reads to say when a chamber's data is stale."""
    now = db.utcnow()
    db.set_sync(conn, f"ingest_attempt:{chamber}", "ok" if ok else "failed", now)
    if ok:
        db.set_sync(conn, f"ingest_ok:{chamber}", None, now)
    conn.commit()


def run_ingest(
    conn: sqlite3.Connection,
    settings: Settings,
    sources: Sources,
    chamber: str | None = None,
    since: date | None = None,
    now: datetime | None = None,
) -> IngestResult:
    now = now or datetime.now(UTC)
    cutoff = (now.date() - timedelta(days=settings.storage.corrections_window_days)).isoformat()
    since_s = since.isoformat() if since else None
    result = IngestResult()

    crosswalk = ensure_crosswalk(conn, settings, sources.http, result, now)
    people = resolve_tracked(crosswalk, settings.members.tracked)

    if sources.congress is None:
        log.error("CONGRESS_GOV_API_KEY is not set: skipping House votes, members and bills")
        result.failed_sources.append("congress_gov")
    else:
        ingest_members(conn, sources.congress, people, settings.congress.number, result, cutoff)

    chambers = [chamber] if chamber else ["house", "senate"]
    for ch in chambers:
        try:
            if ch == "house":
                if sources.congress is None:
                    raise RuntimeError("no Congress.gov API key")
                ingest_house(conn, sources.congress, settings, result, since_s, cutoff)
            elif settings.senate.source == "voteview":
                assert sources.voteview is not None
                ingest_senate_voteview(conn, sources.voteview, settings, result, since_s, cutoff)
            else:
                ingest_senate(conn, sources.senate, settings, result, since_s, cutoff)
            record_chamber_health(conn, ch, ok=True)
        except Exception as e:  # noqa: BLE001 - one chamber never blocks the other
            conn.rollback()
            log.error("%s ingest failed: %s", ch, e, exc_info=True)
            result.failed_sources.append(f"{ch}_votes")
            record_chamber_health(conn, ch, ok=False)

    if sources.congress is not None:
        try:
            ingest_bills(conn, sources.congress, settings, result, now)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            log.error("bill ingest failed: %s", e, exc_info=True)
            result.failed_sources.append("bills")
    return result

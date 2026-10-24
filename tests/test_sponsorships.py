"""Sponsorships, cosponsorship dates and bill text. Uses the shape-only fixtures in
fixtures/congress_gov/unverified/ (invented values; see the README there)."""

from datetime import date

import respx

from conftest import FIXTURES, load_crosswalk_fixtures
from tracker import analyze, db
from tracker.ingest import ingest_cosponsor_dates
from tracker.normalize import run_normalize
from tracker.normalize.common import get_or_create_bill
from tracker.sources.congress_gov import CongressGovClient
from tracker.sources.http import HttpClient
from tracker.summarize import choose_basis, ensure_bill_text

U = FIXTURES / "congress_gov" / "unverified"
WEEK = analyze.week_containing(date(2025, 11, 10), "America/Denver")


def text(name):
    return (U / name).read_text(encoding="utf-8")


def load(conn):
    load_crosswalk_fixtures(conn)
    db.store_raw(conn, "congress_gov", "sponsored", "member/D000197/sponsored/0", text("sponsored_D000197_0.json"))
    db.store_raw(conn, "congress_gov", "cosponsored", "member/D000197/cosponsored/0", text("cosponsored_D000197_0.json"))
    db.store_raw(conn, "congress_gov", "bill_cosponsors", "bill/119/hr/9002/0", text("bill_cosponsors_119_hr_9002_0.json"))


def sponsorships(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT b.bill_type, b.number, s.role, s.date, b.title FROM sponsorship s JOIN bill b ON b.id = s.bill_id"
        " ORDER BY b.number"
    )]


def test_sponsored_and_cosponsored_with_dates(conn, settings):
    load(conn)
    run_normalize(conn, settings)
    assert sponsorships(conn) == [
        ("hr", "9001", "sponsor", "2025-11-12", "Example Clinic Access Act"),
        ("hr", "9002", "cosponsor", "2025-11-12", "Example Watershed Restoration Act"),
    ]  # the amendment and the 118th-Congress bill are skipped; other cosponsors are ignored


def test_rerunning_list_normalize_keeps_cosponsor_date(conn, settings):
    load(conn)
    run_normalize(conn, settings)
    run_normalize(conn, settings, all_records=True)
    assert sponsorships(conn)[1][3] == "2025-11-12"


def test_sponsorships_in_week_become_selected_findings(conn, settings):
    load(conn)
    run_normalize(conn, settings)
    analyze.run_analyze(conn, settings, WEEK)
    rows = conn.execute("SELECT kind, selected, score FROM finding ORDER BY kind").fetchall()
    assert [tuple(r) for r in rows] == [("cosponsored", 1, None), ("sponsored", 1, None)]


def _client():
    http = HttpClient("test/0.1", min_interval_seconds=0, max_attempts=1, sleep=lambda s: None)
    return http, CongressGovClient(http, "TESTKEY")


@respx.mock
def test_cosponsor_list_fetched_once_per_bill(conn):
    route = respx.get("https://api.congress.gov/v3/bill/119/hr/9002/cosponsors").respond(
        200, text=text("bill_cosponsors_119_hr_9002_0.json"))
    http, cg = _client()
    stats = {"cosponsor_lists": 0, "errors": 0}
    ingest_cosponsor_dates(conn, cg, "D000197", [("119", "hr", "9002")], stats)
    ingest_cosponsor_dates(conn, cg, "D000197", [("119", "hr", "9002")], stats)
    http.close()
    assert route.call_count == 1 and stats["cosponsor_lists"] == 1


@respx.mock
def test_bill_text_fetched_for_latest_version_when_no_crs(conn, settings):
    respx.get("https://api.congress.gov/v3/bill/119/hr/9003/text").respond(200, text=text("bill_text_119_hr_9003.json"))
    page = respx.get("https://www.congress.gov/119/bills/hr9003/BILLS-119hr9003rh.htm").respond(
        200, text=text("bill_text_version_119_hr_9003.htm"))
    bid = get_or_create_bill(conn, "119", "hr", "9003")
    http, cg = _client()
    assert ensure_bill_text(conn, cg, bid) is True
    assert ensure_bill_text(conn, cg, bid) is False  # already stored
    http.close()
    assert page.call_count == 1
    basis = choose_basis(conn, bid, 24000)
    assert (basis.kind, basis.version, basis.truncated) == ("bill_text", "2025-11-14", False)
    assert "SEC. 2. GRANT PROGRAM." in basis.text and "<pre>" not in basis.text
    short = choose_basis(conn, bid, 300)
    assert short.truncated and "SEC. 3" not in short.text


@respx.mock
def test_bill_text_skipped_when_crs_exists(conn):
    bid = get_or_create_bill(conn, "119", "hr", "9003")
    conn.execute("INSERT INTO bill_summary VALUES (?, 'crs', '00', '2025-11-01', 'Summary.')", (bid,))
    http, cg = _client()
    assert ensure_bill_text(conn, cg, bid) is False
    http.close()
    assert len(respx.calls) == 0


def test_unverified_fixtures_are_labeled():
    assert "not** recorded from the live API" in (U / "README.md").read_text()

"""Voteview as the Senate source: recorded CSVs (tests/fixtures/voteview) checked against the
senate.gov XML fixtures for the same five votes."""

from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from conftest import FIXTURES, load_crosswalk_fixtures, load_senate_fixtures
from tracker import analyze, db
from tracker.config import SenateConfig
from tracker.ingest import Sources, run_ingest
from tracker.normalize import run_normalize
from tracker.normalize.voteview import display_name, inferred_tie_breaker, vote_date
from tracker.sources.http import HttpClient
from tracker.sources.senate_xml import SenateClient
from tracker.sources.voteview import VoteviewClient

VV = FIXTURES / "voteview"
SHARED = (15, 372, 616, 617, 659)  # recorded from both sources


def load_voteview(conn):
    for kind, name in (("voteview_rollcalls", "S119_rollcalls.csv"), ("voteview_votes", "S119_votes.csv"),
                       ("voteview_members", "S119_members.csv")):
        db.store_raw(conn, "voteview", kind, "S119", (VV / name).read_text(encoding="utf-8"))


def snapshot(conn):
    """Per vote: what both sources must agree on."""
    out = {}
    for v in conn.execute("SELECT * FROM vote_event WHERE chamber='senate' AND session='119-1'"):
        positions = {
            r["bioguide_id"]: (r["position"], r["caucus_at_vote"])
            for r in conn.execute(
                "SELECT l.bioguide_id, mv.position, mv.caucus_at_vote FROM member_vote mv"
                " JOIN legislator l ON l.id = mv.legislator_id WHERE mv.vote_event_id=?",
                (v["id"],),
            )
        }
        bill = conn.execute("SELECT bill_type, number FROM bill WHERE id=?", (v["bill_id"],)).fetchone()
        out[v["roll_number"]] = {
            "question": v["question"], "class": v["vote_class"], "threshold": v["threshold"],
            "result": v["result"], "counts": (v["yea"], v["nay"], v["present"], v["not_voting"]),
            "bill": tuple(bill) if bill else None, "tie": v["tie_breaker"],
            "nomination": v["nomination_ref"] is not None, "day": v["vote_date"][:10], "positions": positions,
        }
    return out


@pytest.fixture
def both(conn, settings):
    """The same votes normalized from senate.gov XML and from Voteview, into separate databases."""
    load_crosswalk_fixtures(conn)
    load_senate_fixtures(conn)
    run_normalize(conn, settings)
    xml = snapshot(conn)

    vv_conn = db.connect(":memory:")
    db.init_db(vv_conn)
    load_crosswalk_fixtures(vv_conn)
    load_voteview(vv_conn)
    settings.senate = SenateConfig(source="voteview")
    stats = run_normalize(vv_conn, settings)
    yield xml, snapshot(vv_conn), vv_conn, stats
    vv_conn.close()


def test_voteview_matches_senate_gov_on_shared_votes(both):
    xml, vv, _, stats = both
    assert stats["voteview_ok"] == 8
    assert "voteview_unmapped" not in stats  # the President's positions are skipped, not unmapped
    for roll in SHARED:
        a, b = xml[roll], vv[roll]
        for key in ("question", "class", "threshold", "result", "counts", "bill", "tie", "nomination", "positions"):
            assert a[key] == b[key], (roll, key)
        assert len(b["positions"]) == 100
    # Voteview publishes only the date, and votes are dated noon Eastern: the day can differ
    # from senate.gov's UTC timestamp for votes after 8 p.m. Eastern, never the Eastern date.
    assert vv[372]["day"] == "2025-07-01"


def test_tie_breakers_inferred_only_when_the_tie_carried(both):
    _, vv, *_ = both
    assert vv[15]["tie"] == vv[372]["tie"] == "Yea"  # Hegseth confirmed, H.R. 1 passed: 50-50 + VP
    assert vv[654]["counts"][:2] == (50, 50) and vv[654]["tie"] is None  # tied and defeated


@pytest.mark.parametrize(
    "yea,nay,result,expected",
    [
        (50, 50, "Bill Passed", "Yea"),
        (50, 50, "Point of Order Well Taken", "Yea"),
        (50, 50, "Joint Resolution Defeated", None),
        (50, 50, "Decision of Chair Not Sustained", None),
        (51, 49, "Bill Passed", None),
        (0, 0, "Agreed to", None),
    ],
)
def test_inferred_tie_breaker(yea, nay, result, expected):
    assert inferred_tie_breaker(yea, nay, result) == expected


def test_other_voteview_rows(both):
    _, vv, conn, _ = both
    table = vv[614]
    assert (table["class"], table["bill"]) == ("table", ("hr", "5371"))
    s2 = conn.execute("SELECT * FROM vote_event WHERE session='119-2' AND roll_number=1").fetchone()
    assert s2 is not None and s2["source_url"].endswith("vote1192/vote_119_2_00001.htm")
    nom = conn.execute("SELECT * FROM vote_event WHERE roll_number=15 AND session='119-1'").fetchone()
    assert nom["vote_title"] == "Peter Hegseth, of Tennessee, to be Secretary of Defense"


def test_unchanged_files_are_not_renormalized(both, settings):
    _, _, conn, _ = both
    assert "voteview_ok" not in run_normalize(conn, settings)
    assert run_normalize(conn, settings, all_records=True)["voteview_ok"] == 8


def test_digest_works_from_voteview(both, settings):
    from tracker import render

    _, _, conn, _ = both
    week = analyze.week_containing(date(2025, 11, 10), "America/Denver")
    analyze.run_analyze(conn, settings, week)
    _, html = render.render_digest(conn, settings, week)
    assert "Vote to end debate on H.R. 5371 (60 votes needed). Passed 60–40." in html
    assert "with 39 of 47 Democratic caucus members" in html
    assert "Senate roll-call votes are from Voteview" in html


def test_nomination_numbers_hidden_for_voteview(both, settings):
    from tracker import render

    _, _, conn, _ = both
    settings.senate = SenateConfig(source="voteview")
    week = analyze.week_containing(date(2025, 1, 24), "America/Denver")
    analyze.run_analyze(conn, settings, week)
    _, html = render.render_digest(conn, settings, week)
    assert "Peter Hegseth, of Tennessee, to be Secretary of Defense" in html and "PN117" not in html


def test_helpers():
    assert vote_date("2025-07-01") == "2025-07-01T16:00:00Z"  # noon EDT
    assert vote_date("2025-01-24") == "2025-01-24T17:00:00Z"  # noon EST
    assert display_name("TUBERVILLE, Thomas Hawley (Tommy)") == "Thomas Hawley Tuberville"


@respx.mock(assert_all_called=False)
def test_ingest_downloads_files_and_queues_bills(respx_mock, conn, settings):
    settings.senate = SenateConfig(source="voteview")
    respx_mock.get("https://unitedstates.github.io/congress-legislators/legislators-current.json").respond(
        200, text=(FIXTURES / "legislators" / "legislators-current.json").read_text())
    respx_mock.get("https://unitedstates.github.io/congress-legislators/legislators-historical.json").respond(
        200, text=(FIXTURES / "legislators" / "legislators-historical.json").read_text())
    files = {f"https://voteview.com/static/data/out/{d}/S119_{d}.csv": (VV / f"S119_{d}.csv").read_text()
             for d in ("rollcalls", "votes", "members")}
    routes = {u: respx_mock.get(u).respond(200, text=t) for u, t in files.items()}
    senate_gov = respx_mock.get(url__regex=r"https://www\.senate\.gov/.*").respond(403)
    http = HttpClient("test/0.1", min_interval_seconds=0, max_attempts=1, sleep=lambda s: None)
    sources = Sources(http=http, congress=None, senate=SenateClient(http),
                      voteview=VoteviewClient(http, settings.senate.voteview_url))
    try:
        result = run_ingest(conn, settings, sources, chamber="senate", since=date(2025, 1, 3),
                            now=datetime(2025, 12, 25, tzinfo=UTC))
        assert result.failed_sources == ["congress_gov"]  # no API key in this test; the Senate is fine
        assert result.stats["voteview"]["changed"] == 3
        assert senate_gov.call_count == 0
        assert db.get_sync(conn, "voteview_votes:119-1")["cursor"] == "659"
        assert db.get_sync(conn, "voteview_votes:119-2")["cursor"] == "1"
        assert {("119", "hr", "1"), ("119", "hr", "5371"), ("119", "sjres", "82")} <= set(result.bill_refs)
        assert not any(t.startswith("pn") for _, t, _ in result.bill_refs)  # nominations are not bills
        second = run_ingest(conn, settings, sources, chamber="senate", now=datetime(2025, 12, 25, tzinfo=UTC))
        assert second.stats["voteview"]["unchanged"] == 3
        assert all(r.call_count == 2 for r in routes.values())
    finally:
        http.close()
    run_normalize(conn, settings)
    assert conn.execute("SELECT COUNT(*) FROM vote_event WHERE chamber='senate'").fetchone()[0] == 8


@respx.mock
def test_non_csv_response_is_rejected(conn, settings):
    from tracker.ingest import IngestResult, ingest_senate_voteview

    respx.get(url__regex=r"https://voteview\.com/.*").mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    http = HttpClient("test/0.1", min_interval_seconds=0, max_attempts=1, sleep=lambda s: None)
    with pytest.raises(ValueError):
        ingest_senate_voteview(conn, VoteviewClient(http, settings.senate.voteview_url), settings,
                               IngestResult(), None, "2025-12-11")
    http.close()
    assert db.get_raw(conn, "voteview", "voteview_rollcalls", "S119") is None

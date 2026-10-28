import json
import re
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from conftest import FIXTURES, fixture_text
from tracker import db
from tracker.ingest import Sources, next_cursor, run_ingest, select_targets
from tracker.normalize import run_normalize
from tracker.sources.congress_gov import CongressGovClient
from tracker.sources.http import HttpClient
from tracker.sources.senate_xml import SenateClient

CG = "https://api.congress.gov/v3"
SENATE_VOTES = "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/"
MENU_ROLLS = [659, 654, 646, 617, 616, 614, 524, 372, 15]


def test_select_targets_first_run_since_and_corrections():
    items = [(1, "2025-01-10"), (2, "2025-06-01"), (3, "2025-12-01"), (4, "2025-12-20")]
    cutoff = "2025-12-11"
    # No cursor, no --since: only the corrections window.
    assert select_targets(items, 0, None, cutoff, set()) == [4]
    # --since backfills everything from that date.
    assert select_targets(items, 0, "2025-01-03", cutoff, set()) == [1, 2, 3, 4]
    # With a cursor: new votes plus the corrections window.
    assert select_targets(items, 3, None, cutoff, {1, 2, 3}) == [4]
    assert select_targets(items, 2, None, cutoff, {1, 2}) == [3, 4]
    # --since again later fills gaps that were never stored.
    assert select_targets(items, 4, "2025-01-03", cutoff, {1, 3, 4}) == [2, 4]


def test_next_cursor_stops_before_failures():
    assert next_cursor(10, [11, 12, 13], []) == 13
    assert next_cursor(10, [11, 13], [12]) == 11
    assert next_cursor(10, [], [5]) == 10


def senate_vote_xml(roll: int) -> str:
    f = FIXTURES / "senate" / f"vote_119_1_{roll:05d}.xml"
    if f.exists():
        return f.read_text()
    # Rolls not recorded as fixtures: reuse a real file under the new number.
    return fixture_text("senate", "vote_119_1_00617.xml").replace("<vote_number>617<", f"<vote_number>{roll}<")


def house_list() -> dict:
    full = json.loads(fixture_text("congress_gov", "house_vote_list_119_1.json"))
    keep = [v for v in full["houseRollCallVotes"] if v["rollCallNumber"] in (242, 245)]
    return {"houseRollCallVotes": keep, "pagination": {"count": 2}}


def house_members(roll: int) -> str:
    return fixture_text("congress_gov", "house_vote_members_119_1_242.json")


def mock_sources(router: respx.MockRouter, senate_up: bool = True) -> dict[str, respx.Route]:
    routes = {}
    router.get("https://unitedstates.github.io/congress-legislators/legislators-current.json").respond(
        200, text=fixture_text("legislators", "legislators-current.json"))
    router.get("https://unitedstates.github.io/congress-legislators/legislators-historical.json").respond(
        200, text=fixture_text("legislators", "legislators-historical.json"))

    router.get(path__regex=r"^/v3/member/[A-Z0-9]+$").respond(200, json={"member": {}})
    router.get(path__regex=r"^/v3/member/[A-Z0-9]+/sponsored-legislation$").respond(
        200, json={"sponsoredLegislation": [
            {"congress": 119, "type": "S", "number": "1234", "introducedDate": "2025-12-20", "title": "A bill"},
            {"congress": 119, "amendmentNumber": "9", "introducedDate": "2025-12-20"},
            {"congress": 118, "type": "S", "number": "1", "introducedDate": "2024-01-01"},
        ], "pagination": {}})
    router.get(path__regex=r"^/v3/member/[A-Z0-9]+/cosponsored-legislation$").respond(
        200, json={"cosponsoredLegislation": [], "pagination": {}})

    routes["house_list"] = router.get(f"{CG}/house-vote/119/1").respond(200, json=house_list())
    router.get(f"{CG}/house-vote/119/2").respond(404, json={"error": "not found"})
    routes["house_detail"] = router.get(path__regex=r"^/v3/house-vote/119/1/\d+$").mock(
        side_effect=lambda req: httpx.Response(200, text=fixture_text(
            "congress_gov", f"house_vote_119_1_{req.url.path.rsplit('/', 1)[1]}.json")))
    routes["house_members"] = router.get(path__regex=r"^/v3/house-vote/119/1/\d+/members$").mock(
        side_effect=lambda req: httpx.Response(200, text=house_members(int(req.url.path.split("/")[-2]))))

    def bill(req):
        m = re.search(r"/bill/119/(\w+)/(\d+)$", req.url.path)
        f = FIXTURES / "congress_gov" / f"bill_119_{m[1]}_{m[2]}.json"
        body = f.read_text() if f.exists() else json.dumps(
            {"bill": {"congress": 119, "type": m[1].upper(), "number": m[2], "title": f"Bill {m[2]}"}})
        return httpx.Response(200, text=body)

    routes["bill"] = router.get(path__regex=r"^/v3/bill/119/\w+/\d+$").mock(side_effect=bill)
    routes["summaries"] = router.get(path__regex=r"^/v3/bill/119/\w+/\d+/summaries$").respond(
        200, json={"summaries": []})

    if senate_up:
        routes["menu"] = router.get(
            "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_1.xml"
        ).respond(200, text=fixture_text("senate", "vote_menu_119_1.xml"))
        routes["senate_vote"] = router.get(url__regex=rf"{re.escape(SENATE_VOTES)}vote_119_1_(\d+)\.xml").mock(
            side_effect=lambda req: httpx.Response(200, text=senate_vote_xml(int(req.url.path[-9:-4]))))
    else:
        routes["menu"] = router.get(
            "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_1.xml").respond(403)
    router.get("https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_2.xml").respond(404)
    return routes


@pytest.fixture
def sources(settings):
    http = HttpClient("test/0.1", min_interval_seconds=0, max_attempts=1, sleep=lambda s: None)
    yield Sources(http=http, congress=CongressGovClient(http, "TESTKEY"), senate=SenateClient(http))
    http.close()


def requested_rolls(route) -> list[int]:
    return sorted(int(re.search(r"(\d+)\.xml$", str(c.request.url))[1]) for c in route.calls)


@respx.mock(assert_all_called=False)
def test_backfill_then_incremental(respx_mock, conn, settings, sources):
    routes = mock_sources(respx_mock)
    first = run_ingest(conn, settings, sources, since=date(2025, 1, 3), now=datetime(2025, 12, 25, tzinfo=UTC))
    assert first.status == "ok", first.as_dict()
    assert requested_rolls(routes["senate_vote"]) == sorted(MENU_ROLLS)
    assert routes["house_detail"].call_count == 2  # 242 and 245
    assert db.get_sync(conn, "senate_votes:119-1")["cursor"] == "659"
    assert db.get_sync(conn, "house_votes:119-1")["cursor"] == "245"
    # Bills: from votes (H.R. 1, H.R. 5371, H.Res. 682, H.R. 3838), rule targets (H.R. 3486), and
    # 119th-Congress sponsorships (S. 1234) - but not the 118th-Congress one or the amendment.
    fetched = {str(c.request.url).split("/v3/")[1].split("?")[0] for c in routes["bill"].calls}
    assert fetched == {"bill/119/hr/1", "bill/119/hr/5371", "bill/119/hres/682", "bill/119/hr/3838",
                       "bill/119/hr/3486", "bill/119/s/1234"}
    raw = conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]
    assert all("TESTKEY" not in (r[0] or "") for r in conn.execute("SELECT url FROM raw_record"))

    # Second run, same day: only the corrections window (Dec 11-25) is re-fetched; nothing new.
    respx_mock.reset()  # clear recorded calls; routes stay
    second = run_ingest(conn, settings, sources, now=datetime(2025, 12, 25, tzinfo=UTC))
    assert second.status == "ok"
    assert requested_rolls(routes["senate_vote"]) == [646, 654, 659]
    assert routes["house_detail"].call_count == 0
    assert second.stats["senate_xml"] == {"unchanged": 3}
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == raw  # no duplicate rows

    # Normalizing after both runs yields one vote_event per roll call.
    run_normalize(conn, settings)
    n = conn.execute("SELECT COUNT(*) FROM vote_event WHERE chamber='senate'").fetchone()[0]
    assert n == len(MENU_ROLLS)


@respx.mock(assert_all_called=False)
def test_one_chamber_failing_does_not_block_the_other(respx_mock, conn, settings, sources):
    mock_sources(respx_mock, senate_up=False)
    result = run_ingest(conn, settings, sources, since=date(2025, 1, 3), now=datetime(2025, 12, 25, tzinfo=UTC))
    assert result.status == "partial"
    assert result.failed_sources == ["senate_votes"]
    assert conn.execute("SELECT COUNT(*) FROM raw_record WHERE kind='house_vote'").fetchone()[0] == 2
    assert db.get_sync(conn, "ingest_attempt:senate")["cursor"] == "failed"
    assert db.get_sync(conn, "ingest_attempt:house")["cursor"] == "ok"


@respx.mock(assert_all_called=False)
def test_unresolvable_tracked_member_fails_fast(respx_mock, conn, settings, sources):
    from tracker.sources.legislators import CrosswalkError

    mock_sources(respx_mock)
    settings.members.tracked = ["X000000"]
    with pytest.raises(CrosswalkError):
        run_ingest(conn, settings, sources, now=datetime(2025, 12, 25, tzinfo=UTC))


@respx.mock
def test_ingest_never_holds_the_write_lock_during_downloads(tmp_path, settings):
    """Other writers (the web app analyzing a week) must never wait behind a download."""
    import sqlite3

    from tracker.ingest import IngestResult, ingest_members
    from tracker.sources.legislators import Person

    path = tmp_path / "lock.db"
    conn = db.connect(path)
    db.init_db(conn)
    blocked = []

    def page(request):
        # Mid-ingest, another connection writes with no willingness to wait at all.
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("INSERT INTO run_log (command, started_at) VALUES ('probe', 'now')")
            other.commit()
        except sqlite3.OperationalError as e:
            blocked.append(str(e))
        finally:
            other.close()
        offset = int(request.url.params.get("offset", 0))
        nxt = {"next": "more"} if offset < 500 else {}
        return httpx.Response(200, json={"cosponsoredLegislation": [], "sponsoredLegislation": [], "pagination": nxt})

    respx.get(path__regex=r"^/v3/member/D000197$").respond(200, json={"member": {}})
    respx.get(path__regex=r"^/v3/member/D000197/(co)?sponsored-legislation$").mock(side_effect=page)
    http = HttpClient("t/0", min_interval_seconds=0, max_attempts=1, sleep=lambda s: None)
    person = Person("D000197", None, "Diana DeGette", "DeGette", "house", "CO", "1")
    ingest_members(conn, CongressGovClient(http, "K"), [person], 119, IngestResult(), "2025-12-11")
    http.close()
    assert blocked == []
    assert conn.execute("SELECT COUNT(*) FROM run_log WHERE command='probe'").fetchone()[0] == 6

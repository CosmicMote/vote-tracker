"""Parsers tested against recorded responses only: totals and caucus splits against the published
party totals, UTC conversion, the amendment target, nominations, Guilty/Not Guilty mapping, and idempotency."""

import json

from conftest import TRACKED, fixture_text, load_crosswalk_fixtures, load_house_fixture
from tracker.normalize import load_crosswalk, run_normalize
from tracker.normalize.common import strip_html
from tracker.normalize.context import NormalizeContext
from tracker.normalize.house import map_positions
from tracker.normalize.house import normalize_vote as normalize_house
from tracker.normalize.senate import menu_date_iso, parse_menu, parse_vote_date
from tracker.normalize.senate import normalize_vote as normalize_senate


def vote(conn, chamber, roll):
    return conn.execute(
        "SELECT * FROM vote_event WHERE chamber=? AND roll_number=?", (chamber, roll)
    ).fetchone()


def caucus_counts(conn, vote_id):
    rows = conn.execute(
        "SELECT caucus_at_vote, position, COUNT(*) n FROM member_vote WHERE vote_event_id=?"
        " GROUP BY 1, 2 ORDER BY 1, 2",
        (vote_id,),
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def test_house_vote_totals_match_party_totals(loaded):
    v = vote(loaded, "house", 242)
    assert (v["question"], v["vote_class"], v["threshold"]) == ("On Ordering the Previous Question", "rule", 0.5)
    assert (v["yea"], v["nay"], v["present"], v["not_voting"]) == (213, 207, 0, 10)
    assert v["vote_date"] == "2025-09-09T18:01:00Z"
    assert v["source_url"] == "https://clerk.house.gov/evs/2025/roll242.xml"
    published = json.loads(fixture_text("congress_gov", "house_vote_119_1_242.json"))["houseRollCallVote"]
    counts = caucus_counts(loaded, v["id"])
    for p in published["votePartyTotal"]:
        party = p["voteParty"]
        assert counts.get((party, "yea"), 0) == p["yeaTotal"]
        assert counts.get((party, "nay"), 0) == p["nayTotal"]
        assert counts.get((party, "not_voting"), 0) == p["notVotingTotal"]
    bill = loaded.execute("SELECT * FROM bill WHERE id=?", (v["bill_id"],)).fetchone()
    assert (bill["bill_type"], bill["number"]) == ("hres", "682")


def test_house_tracked_member_position(loaded):
    row = loaded.execute(
        "SELECT mv.position, mv.position_raw, mv.party_at_vote FROM member_vote mv"
        " JOIN legislator l ON l.id = mv.legislator_id JOIN vote_event v ON v.id = mv.vote_event_id"
        " WHERE l.bioguide_id='D000197' AND v.roll_number=242 AND v.chamber='house'"
    ).fetchone()
    assert tuple(row) == ("nay", "Nay", "D")


def test_house_vote_without_members_is_deferred_not_failed(conn, settings):
    load_crosswalk_fixtures(conn)
    load_house_fixture(conn, 245)  # detail only
    stats = run_normalize(conn, settings)
    assert stats["house_vote_incomplete"] == 1 and stats.get("house_vote_errors", 0) == 0
    assert vote(conn, "house", 245) is None


def test_house_amendment_vote_links_amended_bill(conn):
    load_crosswalk_fixtures(conn)
    ctx = NormalizeContext(conn=conn, crosswalk=load_crosswalk(conn), tracked=set(TRACKED))
    members = fixture_text("congress_gov", "house_vote_members_119_1_242.json")  # any member payload will do
    vid = normalize_house(ctx, fixture_text("congress_gov", "house_vote_119_1_245.json"), members, None)
    v = conn.execute("SELECT * FROM vote_event WHERE id=?", (vid,)).fetchone()
    assert (v["vote_class"], v["amendment_ref"]) == ("amendment", "H.Amdt. 85")
    assert v["vote_title"] == "Norman of South Carolina Part A Amendment No. 13"
    bill = conn.execute("SELECT bill_type, number FROM bill WHERE id=?", (v["bill_id"],)).fetchone()
    assert tuple(bill) == ("hr", "3838")


def test_speaker_election_maps_candidate_names():
    results = [{"voteCast": "Johnson"}] * 3 + [{"voteCast": "Jeffries"}] * 2 + [{"voteCast": "Not Voting"}]
    assert map_positions(results) == {"Johnson": "yea", "Jeffries": "nay", "Not Voting": "not_voting"}
    assert map_positions([{"voteCast": "Aye"}, {"voteCast": "No"}]) == {"Aye": "yea", "No": "nay"}


def test_senate_passage_with_tie_breaker(loaded):
    v = vote(loaded, "senate", 372)
    assert (v["vote_class"], v["threshold"], v["result"]) == ("passage", 0.5, "Bill Passed")
    assert (v["yea"], v["nay"], v["tie_breaker"]) == (50, 50, "Yea")
    assert v["vote_date"] == "2025-07-01T15:56:00Z"  # 11:56 AM EDT
    bill = loaded.execute("SELECT bill_type, number FROM bill WHERE id=?", (v["bill_id"],)).fetchone()
    assert tuple(bill) == ("hr", "1")
    # Sanders and King (independents) are counted with the Democratic caucus.
    counts = caucus_counts(loaded, v["id"])
    assert counts[("D", "nay")] == 47 and counts[("R", "yea")] == 50 and counts[("R", "nay")] == 3


def test_senate_nomination_is_not_a_bill(loaded):
    v = vote(loaded, "senate", 15)
    assert (v["vote_class"], v["nomination_ref"], v["bill_id"]) == ("nomination", "PN11-7", None)
    assert v["vote_title"] == "Confirmation: Peter Hegseth, of Tennessee, to be Secretary of Defense"
    assert v["vote_date"] == "2025-01-25T01:57:00Z"  # 8:57 PM EST


def test_senate_amendment_and_cloture(loaded):
    amd = vote(loaded, "senate", 616)
    clo = vote(loaded, "senate", 617)
    assert (amd["vote_class"], amd["amendment_ref"]) == ("amendment", "S.Amdt. 3937")
    assert amd["bill_id"] == clo["bill_id"]
    assert (clo["vote_class"], clo["threshold"], clo["yea"], clo["nay"]) == ("cloture", 0.6, 60, 40)
    counts = caucus_counts(loaded, clo["id"])
    assert counts[("D", "yea")] == 8 and counts[("D", "nay")] == 39


def test_senate_cloture_on_nomination_is_simple_majority(loaded):
    v = vote(loaded, "senate", 659)
    assert (v["vote_class"], v["threshold"], v["nomination_ref"]) == ("cloture", 0.5, "PN373")
    assert (v["yea"], v["nay"], v["not_voting"]) == (51, 42, 7)
    n = loaded.execute(
        "SELECT COUNT(*) FROM member_vote WHERE vote_event_id=? AND position='not_voting'", (v["id"],)
    ).fetchone()[0]
    assert n == 7


def test_impeachment_positions_keep_raw_string(conn):
    load_crosswalk_fixtures(conn)
    ctx = NormalizeContext(conn=conn, crosswalk=load_crosswalk(conn), tracked=set(TRACKED))
    xml = fixture_text("senate", "vote_119_1_00617.xml").replace(">Yea<", ">Guilty<").replace(">Nay<", ">Not Guilty<")
    vid = normalize_senate(ctx, xml, None)
    rows = conn.execute(
        "SELECT position, position_raw, COUNT(*) FROM member_vote WHERE vote_event_id=? GROUP BY 1, 2", (vid,)
    ).fetchall()
    assert sorted(tuple(r) for r in rows) == [("nay", "Not Guilty", 40), ("yea", "Guilty", 60)]


def test_normalize_is_idempotent(loaded, settings):
    before = {t: loaded.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("legislator", "vote_event", "member_vote", "bill", "bill_summary", "rule_link", "party_membership")}
    stats = run_normalize(loaded, settings)
    assert not any(k.endswith("_ok") for k in stats)  # nothing changed, nothing re-parsed
    run_normalize(loaded, settings, all_records=True)
    after = {t: loaded.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after


def test_crs_summary_html_is_stripped(loaded):
    text = loaded.execute("SELECT text FROM bill_summary").fetchone()[0]
    assert "<p>" not in text and "&nbsp;" not in text
    assert text.startswith("Shared Property Agency Collaboration and Engagement Act of 2025")


def test_menu_parsing():
    items = parse_menu(fixture_text("senate", "vote_menu_119_1.xml"))
    by_roll = {i["roll"]: i for i in items}
    assert by_roll["616"]["question"] == "On the Amendment"
    assert menu_date_iso(by_roll["616"]["date"]) == "2025-11-10"
    assert menu_date_iso(by_roll["15"]["date"]) == "2025-01-24"


def test_date_and_html_helpers():
    assert parse_vote_date("December 18, 2025,  09:42 PM") == "2025-12-19T02:42:00Z"
    assert strip_html("<p>A&nbsp;b</p><p>c</p>") == "A b\n\nc"

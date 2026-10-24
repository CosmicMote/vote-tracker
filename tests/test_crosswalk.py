import logging

import pytest

from conftest import TRACKED, fixture_text, load_crosswalk_fixtures
from tracker.normalize import load_crosswalk, run_normalize
from tracker.normalize.context import NormalizeContext
from tracker.normalize.senate import normalize_vote
from tracker.sources.legislators import Crosswalk, CrosswalkError


@pytest.fixture
def crosswalk() -> Crosswalk:
    return Crosswalk.from_json(
        fixture_text("legislators", "legislators-current.json"),
        fixture_text("legislators", "legislators-historical.json"),
        since="2025-01-03",
    )


def test_tracked_members_resolve_to_chamber_and_lis(crosswalk):
    rep, bennet, hick = (crosswalk.resolve(b) for b in TRACKED)
    assert (rep.chamber, rep.state, rep.district, rep.lis) == ("house", "CO", "1", None)
    assert (bennet.chamber, bennet.lis) == ("senate", "S330")
    assert (hick.chamber, hick.lis) == ("senate", "S408")
    assert crosswalk.from_lis("S330").bioguide == "B001267"


def test_unknown_or_former_member_cannot_be_tracked(crosswalk):
    with pytest.raises(CrosswalkError):
        crosswalk.resolve("X000000")
    # Lindsey Graham left the Senate in 2026: mappable for old votes, but not trackable.
    assert crosswalk.from_lis("S293").bioguide == "G000359"
    with pytest.raises(CrosswalkError):
        crosswalk.resolve("G000359")


def test_independent_caucuses_with_democrats(crosswalk):
    sanders = crosswalk.from_lis("S313")
    assert (sanders.party, sanders.caucus) == ("I", "D")
    assert crosswalk.caucus_on(sanders.bioguide, "2025-07-01", "I") == "D"


def test_mid_term_party_switch_is_dated(crosswalk):
    # Rep. Kiley: Republican until 2026-03-08, then an independent caucusing with Republicans.
    kiley = crosswalk.by_bioguide["K000401"]
    assert kiley.affiliation_on("2025-06-01").party == "R"
    after = kiley.affiliation_on("2026-04-01")
    assert (after.party, after.caucus) == ("I", "R")
    assert crosswalk.caucus_on("K000401", "2026-04-01", "I") == "R"


def test_speaker_detected_from_leadership_roles(crosswalk):
    assert crosswalk.by_bioguide["J000299"].is_speaker_on("2025-11-12")
    assert not crosswalk.by_bioguide["D000197"].is_speaker_on("2025-11-12")


def test_unmapped_lis_is_logged_and_skipped(conn, settings, caplog):
    load_crosswalk_fixtures(conn)
    ctx = NormalizeContext(conn=conn, crosswalk=load_crosswalk(conn), tracked=set(TRACKED))
    xml = fixture_text("senate", "vote_119_1_00617.xml").replace("<lis_member_id>S330<", "<lis_member_id>S999<")
    with caplog.at_level(logging.WARNING):
        vote_id = normalize_vote(ctx, xml, None)
    assert "S999" in caplog.text
    assert ctx.stats["senate_unmapped_lis"] == 1
    n = conn.execute("SELECT COUNT(*) FROM member_vote WHERE vote_event_id=?", (vote_id,)).fetchone()[0]
    assert n == 99
    assert conn.execute("SELECT COUNT(*) FROM legislator WHERE lis_id='S999'").fetchone()[0] == 0


def test_without_historical_file_departed_senators_are_unmapped(conn, settings):
    from tracker import db

    db.store_raw(conn, "legislators", "legislators_current", "current",
                 fixture_text("legislators", "legislators-current.json"))
    ctx = NormalizeContext(conn=conn, crosswalk=load_crosswalk(conn), tracked=set(TRACKED))
    normalize_vote(ctx, fixture_text("senate", "vote_119_1_00617.xml"), None)
    assert dict(ctx.unmapped_lis) == {"S293": 1, "S419": 1}


def test_normalize_sets_tracked_flag(conn, settings):
    load_crosswalk_fixtures(conn)
    run_normalize(conn, settings)
    rows = conn.execute("SELECT bioguide_id, lis_id, chamber FROM legislator WHERE tracked=1 ORDER BY bioguide_id").fetchall()
    assert [tuple(r) for r in rows] == [("B001267", "S330", "senate"), ("D000197", None, "house"), ("H000273", "S408", "senate")]

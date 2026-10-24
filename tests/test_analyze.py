"""Synthetic votes: party breaks, split caucuses, ties, present vs. not voting, lopsided votes,
threshold-relative closeness, collapsing, delegation splits, and re-run determinism."""

import json
from datetime import date

import pytest

from tracker import analyze
from tracker.analyze import (
    caucus_position,
    closeness,
    is_party_break,
    score_vote,
    week_containing,
)
from tracker.config import ScoringConfig

CFG = ScoringConfig()
WEEK = week_containing(date(2025, 11, 10), "America/Denver")


class World:
    """A tiny Senate: tracked senators A and B (Democrats), more Democrats, Republicans, and an independent."""

    def __init__(self, conn):
        self.conn = conn
        self.ids: dict[str, int] = {}
        self.caucus: dict[str, str] = {}
        self.roll = 0
        self.add("A", "D", tracked=True)
        self.add("B", "D", tracked=True)
        for i in range(8):
            self.add(f"D{i}", "D")
        for i in range(10):
            self.add(f"R{i}", "R")
        self.add("I0", "D", party="I")

    def add(self, key, caucus, tracked=False, party=None):
        cur = self.conn.execute(
            "INSERT INTO legislator (bioguide_id, lis_id, name, level, chamber, state, tracked)"
            " VALUES (?, ?, ?, 'federal', 'senate', 'CO', ?)",
            (f"{key}000001", f"S{key}", f"Senator {key}", int(tracked)),
        )
        self.ids[key] = cur.lastrowid
        self.caucus[key] = (party or caucus, caucus)

    def vote(self, positions: dict[str, str], *, vote_class="passage", threshold=0.5, bill_id=None,
             nomination_ref=None, tie_breaker=None, day="2025-11-12T18:00:00Z", default=None):
        """positions: key -> yea|nay|present|not_voting. Unlisted members get `default` (or no row)."""
        self.roll += 1
        rows = dict(positions)
        if default:
            for k in self.ids:
                rows.setdefault(k, default)
        yea = sum(1 for p in rows.values() if p == "yea")
        nay = sum(1 for p in rows.values() if p == "nay")
        cur = self.conn.execute(
            "INSERT INTO vote_event (jurisdiction, chamber, session, roll_number, vote_date, question, vote_class,"
            " threshold, result, bill_id, nomination_ref, tie_breaker, yea, nay, present, not_voting)"
            " VALUES ('us', 'senate', '119-1', ?, ?, 'On Passage of the Bill', ?, ?, 'Passed', ?, ?, ?, ?, ?, 0, 0)",
            (self.roll, day, vote_class, threshold, bill_id, nomination_ref, tie_breaker, yea, nay),
        )
        vid = cur.lastrowid
        for k, p in rows.items():
            party, caucus = self.caucus[k]
            self.conn.execute(
                "INSERT INTO member_vote VALUES (?, ?, ?, ?, ?, ?)", (vid, self.ids[k], p, p.title(), party, caucus)
            )
        return vid

    def bill(self, number):
        cur = self.conn.execute(
            "INSERT INTO bill (jurisdiction, session, bill_type, number, title) VALUES ('us', '119', 's', ?, ?)",
            (str(number), f"A bill {number}"),
        )
        return cur.lastrowid

    def dems(self, n_yea, rest="nay"):
        """Positions for the eight untracked Democrats plus the independent: n_yea yea, the rest `rest`."""
        keys = [f"D{i}" for i in range(8)] + ["I0"]
        return {k: ("yea" if i < n_yea else rest) for i, k in enumerate(keys)}

    def reps(self, pos):
        return {f"R{i}": pos for i in range(10)}

    def findings(self, key, kind=None):
        sql = "SELECT * FROM finding WHERE legislator_id=?"
        args = [self.ids[key]]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()


@pytest.fixture
def world(conn):
    return World(conn)


def run(world, settings):
    return analyze.run_analyze(world.conn, settings, WEEK)


# --- pure functions -------------------------------------------------------------


def test_caucus_position_tie_has_no_majority():
    cp = caucus_position("D", 5, 5)
    assert cp.majority is None and cp.cohesion == 0.5
    assert not is_party_break("yea", cp, CFG)


def test_cloture_59_41_scores_as_close_but_simple_majority_does_not():
    cloture = closeness(59, 41, 0.6)
    simple = closeness(59, 41, 0.5)
    assert cloture == pytest.approx(1 + (1 - 0.01 / 0.6))  # one point off the 60% line
    assert simple == pytest.approx(1 + (1 - 0.09 / 0.5))  # nine points past a simple majority
    assert cloture > simple
    assert score_vote("cloture", 0.6, 59, 41, party_break=False, sponsored=False, tie_breaker=False,
                      policy_area=None, cfg=CFG) == pytest.approx(0.9 * cloture, abs=1e-4)


def test_score_bonuses_and_topic_boost():
    cfg = ScoringConfig(topic_boost={"Taxation": 0.25})
    base = score_vote("passage", 0.5, 50, 50, party_break=False, sponsored=False, tie_breaker=False,
                      policy_area=None, cfg=cfg)
    assert base == pytest.approx(2.0)
    full = score_vote("passage", 0.5, 50, 50, party_break=True, sponsored=True, tie_breaker=True,
                      policy_area="Taxation", cfg=cfg)
    assert full == pytest.approx(2.0 + 1.5 + 0.5 + 0.5 + 0.25)


# --- findings -----------------------------------------------------------------


def test_party_break_against_united_caucus(world, settings):
    # Democrats 9-1 nay (cohesion 0.9); A votes yea.
    vid = world.vote({"A": "yea", "B": "nay", **world.dems(0), **world.reps("yea")})
    run(world, settings)
    breaks = world.findings("A", "party_break")
    assert [f["vote_event_id"] for f in breaks] == [vid]
    assert breaks[0]["selected"] == 1
    detail = json.loads(breaks[0]["detail_json"])
    assert detail["caucus_majority"] == "nay" and detail["caucus_split"] == "1-10"
    assert world.findings("B", "party_break") == []


def test_break_from_split_caucus_is_not_flagged(world, settings):
    # Democrats 5 yea / 6 nay (cohesion 0.55): A's yea is not a break.
    world.vote({"A": "yea", "B": "nay", **world.dems(4), **world.reps("nay")})
    run(world, settings)
    assert world.findings("A", "party_break") == []


def test_small_caucus_skips_break_detection(world, settings):
    cp = caucus_position("I", 0, 4)
    assert not is_party_break("yea", cp, CFG)


def test_present_is_not_missed_and_not_voting_is(world, settings):
    world.vote({"A": "present", "B": "not_voting", **world.dems(9), **world.reps("nay")})
    run(world, settings)
    assert world.findings("A", "missed_vote") == []
    assert len(world.findings("B", "missed_vote")) == 1
    stats_a = analyze.member_stats(world.conn, world.conn.execute(
        "SELECT * FROM legislator WHERE id=?", (world.ids["A"],)).fetchone(), WEEK, CFG)
    assert stats_a["cast"] == 1 and stats_a["present"] == 1 and stats_a["missed"] == 0


def test_member_not_seated_is_not_missed(world, settings):
    # No member_vote row for A at all (e.g. sworn in later in the week).
    world.vote({"B": "yea", **world.dems(9), **world.reps("nay")})
    run(world, settings)
    assert world.findings("A") == []


def test_lopsided_vote_excluded_unless_sponsored(world, settings):
    lopsided = world.vote({"A": "yea", "B": "yea", **world.dems(9), **world.reps("yea")})
    run(world, settings)
    (f,) = world.findings("A", "vote")
    assert f["vote_event_id"] == lopsided and f["selected"] == 0
    assert json.loads(f["detail_json"])["lopsided"] is True

    # Same lopsided vote on a bill A cosponsored: now selected.
    bill = world.bill(42)
    world.conn.execute("UPDATE vote_event SET bill_id=? WHERE id=?", (bill, lopsided))
    world.conn.execute("INSERT INTO sponsorship VALUES (?, ?, 'cosponsor', '2025-01-10', NULL)", (bill, world.ids["A"]))
    run(world, settings)
    (f,) = world.findings("A", "vote")
    assert f["selected"] == 1
    assert json.loads(f["detail_json"])["sponsored"] is True


def test_housekeeping_never_notable(world, settings):
    world.vote({"A": "yea", "B": "yea", **world.dems(0), **world.reps("nay")}, vote_class="housekeeping")
    run(world, settings)
    assert world.findings("A", "vote")[0]["selected"] == 0


def test_votes_on_same_bill_collapse_and_top_n_counts_items(world, settings):
    settings.digest.top_n = 1
    bill = world.bill(7)
    cloture = world.vote({"A": "yea", "B": "yea", **world.dems(9), **world.reps("nay")},
                         vote_class="cloture", threshold=0.6, bill_id=bill)
    passage = world.vote({"A": "yea", "B": "yea", **world.dems(9), **world.reps("nay")}, bill_id=bill)
    other = world.vote({"A": "yea", "B": "yea", **world.dems(9), **world.reps("nay")}, vote_class="amendment")
    run(world, settings)
    selected = {f["vote_event_id"] for f in world.findings("A", "vote") if f["selected"]}
    assert selected == {cloture, passage}
    assert other not in selected


def test_delegation_split(world, settings):
    split = world.vote({"A": "yea", "B": "nay", **world.dems(5), **world.reps("nay")})
    world.vote({"A": "yea", "B": "present", **world.dems(5), **world.reps("nay")})  # present is not a split
    run(world, settings)
    splits = analyze.delegation_splits(world.conn, WEEK)
    assert [s["vote"]["id"] for s in splits] == [split]


def test_votes_outside_week_are_ignored(world, settings):
    world.vote({"A": "yea", "B": "nay", **world.dems(0), **world.reps("yea")}, day="2025-11-17T07:30:00Z")  # Mon 00:30 MST
    world.vote({"A": "yea", "B": "nay", **world.dems(0), **world.reps("yea")}, day="2025-11-10T06:59:00Z")  # Sun 23:59 MST
    run(world, settings)
    assert world.findings("A") == []


def test_rerun_replaces_findings_identically(world, settings):
    world.vote({"A": "yea", "B": "nay", **world.dems(0), **world.reps("yea")})
    world.vote({"A": "not_voting", "B": "yea", **world.dems(9), **world.reps("nay")}, vote_class="cloture", threshold=0.6)
    run(world, settings)
    first = [tuple(r)[1:] for r in world.conn.execute("SELECT * FROM finding ORDER BY id")]
    run(world, settings)
    second = [tuple(r)[1:] for r in world.conn.execute("SELECT * FROM finding ORDER BY id")]
    assert first == second


def test_missed_vote_listed_only_above_min_score(world, settings):
    settings.digest.missed_vote_min_score = 1.5
    # 10-10: a suspension (0.3 x 1.75 = 0.525) is listed only in the count; a passage (1.0 x 2.0) is listed.
    world.vote({"A": "not_voting", "B": "yea", **world.dems(9), **world.reps("nay")}, vote_class="suspension",
               threshold=0.6667)
    world.vote({"A": "not_voting", "B": "yea", **world.dems(9), **world.reps("nay")}, threshold=0.5)
    run(world, settings)
    missed = world.findings("A", "missed_vote")
    assert len(missed) == 2
    assert [m["selected"] for m in missed] == [0, 1]


def test_week_boundaries():
    p = week_containing(date(2025, 11, 13), "America/Denver")
    assert (p.start, p.end) == (date(2025, 11, 10), date(2025, 11, 16))
    assert p.start_utc == "2025-11-10T07:00:00Z" and p.end_utc == "2025-11-17T07:00:00Z"

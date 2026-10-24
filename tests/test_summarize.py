import json
from datetime import date

import pytest

from tracker import analyze, db
from tracker.normalize.common import get_or_create_bill
from tracker.summarize import (
    PROVENANCE,
    choose_basis,
    run_summarize,
    summarize_bill,
    summary_view,
    truncate_at_section,
)

VALID = json.dumps(
    {
        "plain_title": "Extends federal grazing permit terms",
        "what_it_does": [
            "Extends permit terms on federal grazing land from 10 to 20 years.",
            "Requires the agency to process renewals within 180 days.",
        ],
        "caveat": None,
    }
)
INVALID = json.dumps({"plain_title": "x", "what_it_does": ["only one bullet"], "caveat": None})


class FakeProvider:
    name = "fake:test"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system, user, schema):
        self.calls.append((system, user))
        return self.responses.pop(0)


def add_bill(conn, number="1234", crs=True, title="A bill to amend the grazing laws."):
    bid = get_or_create_bill(conn, "119", "s", number)
    conn.execute("UPDATE bill SET title=?, policy_area='Public Lands and Natural Resources' WHERE id=?", (title, bid))
    if crs:
        conn.execute(
            "INSERT INTO bill_summary VALUES (?, 'crs', '00', '2025-03-01', ?)",
            (bid, "Grazing Act\nThis bill extends permits. It also sets deadlines. A third sentence."),
        )
    return bid


def test_valid_json_is_stored_and_cached(conn, settings):
    bid = add_bill(conn)
    p = FakeProvider(VALID)
    assert summarize_bill(conn, p, bid, settings) == "created"
    assert summarize_bill(conn, p, bid, settings) == "cached"
    assert len(p.calls) == 1
    row = conn.execute("SELECT * FROM llm_summary").fetchone()
    assert (row["basis"], row["basis_version"], row["prompt_version"], row["model"]) == ("crs", "00", "v1", "fake:test")
    view = summary_view(conn, bid, settings)
    assert view["plain_title"] == "Extends federal grazing permit terms"
    assert view["provenance"] == PROVENANCE["crs"]


def test_prompt_never_mentions_votes(conn, settings):
    bid = add_bill(conn)
    p = FakeProvider(VALID)
    summarize_bill(conn, p, bid, settings)
    system, user = p.calls[0]
    for word in ("voted", "yea", "nay", "senator", "party"):
        assert word not in user.lower()
    assert "S. 1234" in user and "Congressional Research Service summary" in user


def test_invalid_then_valid_retries_with_error(conn, settings):
    bid = add_bill(conn)
    p = FakeProvider(INVALID, VALID)
    assert summarize_bill(conn, p, bid, settings) == "created"
    assert len(p.calls) == 2
    assert "previous response was rejected" in p.calls[1][1]


def test_invalid_twice_stores_nothing_and_falls_back(conn, settings):
    bid = add_bill(conn)
    p = FakeProvider(INVALID, "not json at all")
    assert summarize_bill(conn, p, bid, settings) == "failed"
    assert conn.execute("SELECT COUNT(*) FROM llm_summary").fetchone()[0] == 0
    view = summary_view(conn, bid, settings)
    assert view["excerpt"] == "This bill extends permits. It also sets deadlines."
    assert view["provenance"] == PROVENANCE["crs_excerpt"]


def test_title_only_never_calls_provider(conn, settings):
    bid = add_bill(conn, crs=False)
    p = FakeProvider()
    assert summarize_bill(conn, p, bid, settings) == "title_only"
    assert p.calls == []
    view = summary_view(conn, bid, settings)
    assert view["provenance"] == "No summary or text published yet"
    assert view["plain_title"] == "A bill to amend the grazing laws."


def test_bill_text_basis_when_no_crs(conn, settings):
    bid = add_bill(conn, crs=False)
    db.store_raw(conn, "congress_gov", "bill_text_version", "bill/119/s/1234/2025-02-01",
                 "<html><body><pre>SEC. 1. SHORT TITLE.\nThis Act may be cited as the Grazing Act.</pre></body></html>")
    basis = choose_basis(conn, bid, 24000)
    assert (basis.kind, basis.version) == ("bill_text", "2025-02-01")
    p = FakeProvider(VALID)
    summarize_bill(conn, p, bid, settings)
    assert "Material provided: Bill text" in p.calls[0][1]
    assert summary_view(conn, bid, settings)["provenance"] == PROVENANCE["bill_text"]


def test_new_prompt_version_regenerates(conn, settings):
    bid = add_bill(conn)
    p = FakeProvider(VALID, VALID)
    summarize_bill(conn, p, bid, settings)
    settings.llm.prompt_version = "v2"
    assert summarize_bill(conn, p, bid, settings) == "created"
    assert len(p.calls) == 2


def test_truncates_at_section_boundary():
    text = "SEC. 1. TITLE.\n" + "a" * 50 + "\nSEC. 2. RULES.\n" + "b" * 50 + "\nSEC. 3. MORE.\n" + "c" * 50
    cut, truncated = truncate_at_section(text, 100)
    assert truncated
    assert cut == "SEC. 1. TITLE.\n" + "a" * 50  # cut before SEC. 2, not mid-section
    assert truncate_at_section("short", 100) == ("short", False)


def _vote(conn, chamber, roll, bill_id=None, nomination_ref=None):
    cur = conn.execute(
        "INSERT INTO vote_event (jurisdiction, chamber, session, roll_number, vote_date, question, vote_class,"
        " threshold, bill_id, nomination_ref, yea, nay) VALUES ('us', ?, '119-1', ?, '2025-11-12T18:00:00Z',"
        " 'On Passage', 'passage', 0.5, ?, ?, 50, 49)",
        (chamber, roll, bill_id, nomination_ref),
    )
    return cur.lastrowid


def _finding(conn, leg, vote_id, bill_id, period):
    conn.execute(
        "INSERT INTO finding (legislator_id, period_start, period_end, kind, vote_event_id, bill_id, score, selected)"
        " VALUES (?, ?, ?, 'vote', ?, ?, 2.0, 1)",
        (leg, period.start.isoformat(), period.end.isoformat(), vote_id, bill_id),
    )


@pytest.fixture
def week():
    return analyze.week_containing(date(2025, 11, 10), "America/Denver")


def test_bill_voted_in_both_chambers_summarized_once_and_nominations_skipped(conn, settings, week):
    conn.execute("INSERT INTO legislator (id, name, level, chamber) VALUES (1, 'Rep', 'federal', 'house')")
    conn.execute("INSERT INTO legislator (id, name, level, chamber) VALUES (2, 'Sen', 'federal', 'senate')")
    bid = add_bill(conn)
    _finding(conn, 1, _vote(conn, "house", 10, bill_id=bid), bid, week)
    _finding(conn, 2, _vote(conn, "senate", 20, bill_id=bid), bid, week)
    _finding(conn, 2, _vote(conn, "senate", 21, nomination_ref="PN11-7"), None, week)
    p = FakeProvider(VALID)
    stats = run_summarize(conn, settings, week, p)
    assert stats == {"created": 1}
    assert len(p.calls) == 1
    assert "PN11-7" not in p.calls[0][1]


def test_rule_vote_summarizes_underlying_bill(conn, settings, week):
    conn.execute("INSERT INTO legislator (id, name, level, chamber) VALUES (1, 'Rep', 'federal', 'house')")
    rule = get_or_create_bill(conn, "119", "hres", "682")
    under = add_bill(conn, "3838")
    conn.execute("INSERT INTO rule_link VALUES (?, ?)", (rule, under))
    _finding(conn, 1, _vote(conn, "house", 30, bill_id=rule), rule, week)
    p = FakeProvider(VALID)
    run_summarize(conn, settings, week, p)
    assert len(p.calls) == 1 and "S. 3838" in p.calls[0][1]
    assert conn.execute("SELECT bill_id FROM llm_summary").fetchone()[0] == under

"""Snapshot of the digest for a fixed fixture database: one representative and two senators,
week of Nov 10, 2025 (H.R. 5371, the continuing resolution, voted on in both chambers)."""

import json
import os
from datetime import date
from pathlib import Path

import pytest

from conftest import FETCHED_AT, FIXTURES
from tracker import analyze, db, render
from tracker.normalize import run_normalize
from tracker.summarize import run_summarize

SNAPSHOTS = Path(__file__).parent / "snapshots"
WEEK = analyze.week_containing(date(2025, 11, 10), "America/Denver")
RULE_WEEK = analyze.week_containing(date(2025, 9, 9), "America/Denver")  # House rule vote, roll 242


class EchoProvider:
    """Deterministic fake: a summary derived from the bill number in the prompt."""

    name = "fake:snapshot"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        number = user.splitlines()[0].removeprefix("Bill number: ")
        return json.dumps({
            "plain_title": f"Plain-language title for {number}",
            "what_it_does": [f"First thing {number} would do.", f"Second thing {number} would do."],
            "caveat": None,
        })


@pytest.fixture
def digest_db(loaded, settings):
    conn = loaded
    # Sponsorships dated in the week (shape-only fixtures with invented bills).
    u = FIXTURES / "congress_gov" / "unverified"
    for kind, name in (("sponsored", "sponsored_D000197_0.json"), ("cosponsored", "cosponsored_D000197_0.json")):
        db.store_raw(conn, "congress_gov", kind, f"member/D000197/{kind}/0", (u / name).read_text(),
                     fetched_at=FETCHED_AT)
    db.store_raw(conn, "congress_gov", "bill_cosponsors", "bill/119/hr/9002/0",
                 (u / "bill_cosponsors_119_hr_9002_0.json").read_text(), fetched_at=FETCHED_AT)
    run_normalize(conn, settings)
    for week in (WEEK, RULE_WEEK):
        analyze.run_analyze(conn, settings, week)
        run_summarize(conn, settings, week, EchoProvider())
    return conn


def check_snapshot(name, html):
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(html, encoding="utf-8")
    assert html == path.read_text(encoding="utf-8")


def test_digest_snapshot(digest_db, settings):
    subject, html = render.render_digest(digest_db, settings, WEEK)
    check_snapshot("digest_2025-11-10.html", html)
    assert subject == "Your delegation — week of Nov 10, 2025: 2 notable votes, 0 breaks from party"


def test_rule_week_snapshot(digest_db, settings):
    _, html = render.render_digest(digest_db, settings, RULE_WEEK)
    check_snapshot("digest_2025-09-08.html", html)
    assert "Procedural vote to begin debate on the rule (H.Res. 682) for H.R. 3838 and H.R. 3486." in html
    assert "Rep. Diana DeGette voted Nay, with 207 of 207 Democratic caucus members." in html


def test_digest_structure(digest_db, settings):
    _, html = render.render_digest(digest_db, settings, WEEK)
    # Representative first, then senators by last name.
    assert html.index("Rep. Diana DeGette") < html.index("Sen. Michael F. Bennet") < html.index("Sen. John W. Hickenlooper")
    assert "Vote to end debate on H.R. 5371 (60 votes needed). Passed 60–40." in html
    assert "with 39 of 47 Democratic caucus members" in html
    assert "Example Clinic Access Act" in html and "Cosponsored" in html
    assert "Voice votes and unanimous consent" in html
    assert "<style" not in html  # inline styles only
    # A bill shown for one member is referenced, not repeated, for the next.
    assert "see the <a href=\"#bill-" in html


def test_rendering_twice_is_identical(digest_db, settings):
    assert render.render_digest(digest_db, settings, WEEK) == render.render_digest(digest_db, settings, WEEK)


def test_stale_chamber_warning(digest_db, settings):
    from tracker import db

    db.set_sync(digest_db, "ingest_attempt:senate", "failed", "2025-11-17T09:00:00Z")
    db.set_sync(digest_db, "ingest_ok:senate", None, "2025-11-15T09:00:00Z")
    _, html = render.render_digest(digest_db, settings, WEEK)
    assert "Senate data may be stale" in html and "Nov 15, 2025" in html


def test_write_digest_names_file_for_monday(digest_db, settings):
    _, path = render.write_digest(digest_db, settings, WEEK)
    assert path.name == "2025-11-10.html" and path.exists()

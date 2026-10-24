import json

import pytest

from conftest import fixture_text
from tracker.normalize.bills import normalize_bill
from tracker.normalize.common import find_bill_refs, is_rule_title, parse_bill_ref, rule_targets
from tracker.normalize.context import NormalizeContext


def test_rule_referencing_one_bill():
    title = (
        "Providing for consideration of the bill (H.R. 1) to provide for reconciliation pursuant to "
        "title II of H. Con. Res. 14."
    )
    assert is_rule_title("hres", title)
    # H. Con. Res. 14 is cited only as background, not brought to the floor.
    assert rule_targets("377", title) == [("hr", "1")]


def test_rule_referencing_several_bills_from_fixture():
    bill = json.loads(fixture_text("congress_gov", "bill_119_hres_682.json"))["bill"]
    assert is_rule_title("hres", bill["title"])
    assert rule_targets("682", bill["title"]) == [("hr", "3838"), ("hr", "3486")]


def test_rule_referencing_senate_bill_and_joint_resolution():
    title = (
        "Providing for consideration of the bill (S. 5) to require the Secretary of Homeland Security to "
        "take into custody aliens who have been charged; and providing for consideration of the joint "
        "resolution (H.J. Res. 20) providing for congressional disapproval under chapter 8 of title 5, "
        "United States Code, of the rule submitted by the Department of Energy."
    )
    assert rule_targets("93", title) == [("s", "5"), ("hjres", "20")]


def test_non_rule_resolution_is_not_a_rule():
    assert not is_rule_title("hres", "Electing Members to certain standing committees of the House.")
    assert not is_rule_title("hr", "Providing for consideration of something")


def test_rule_without_parenthesized_citation_falls_back_to_all_refs():
    title = "Providing for consideration of H.R. 7, and for other purposes; and relating to H. Res. 12."
    assert rule_targets("12", title) == [("hr", "7")]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("H.R. 1234", ("hr", "1234")),
        ("S. 56", ("s", "56")),
        ("S.J.Res. 7", ("sjres", "7")),
        ("S. J. Res. 82", ("sjres", "82")),
        ("H.Res. 682", ("hres", "682")),
        ("H. Con. Res. 14", ("hconres", "14")),
        ("S.Con.Res. 7", ("sconres", "7")),
        ("HJRES 78", ("hjres", "78")),
        ("SJRES 18", ("sjres", "18")),
        ("HR 3424", ("hr", "3424")),
        ("PN11-7", None),
        ("S.Amdt. 3937", None),
    ],
)
def test_parse_bill_ref(text, expected):
    assert parse_bill_ref(text) == expected


def test_us_code_is_not_a_senate_bill():
    assert find_bill_refs("under 5 U.S. 5 and section 5") == []


def test_normalize_bill_writes_rule_links(conn):
    ctx = NormalizeContext(conn=conn, crosswalk=None)
    rule_id = normalize_bill(ctx, fixture_text("congress_gov", "bill_119_hres_682.json"))
    rows = conn.execute(
        "SELECT b.bill_type, b.number FROM rule_link r JOIN bill b ON b.id = r.underlying_bill_id"
        " WHERE r.rule_bill_id=? ORDER BY b.number",
        (rule_id,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [("hr", "3486"), ("hr", "3838")]
    # Re-running is idempotent.
    normalize_bill(ctx, fixture_text("congress_gov", "bill_119_hres_682.json"))
    assert conn.execute("SELECT COUNT(*) FROM rule_link").fetchone()[0] == 2

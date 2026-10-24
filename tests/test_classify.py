import pytest

from tracker.classify import classify, parse_majority_requirement, threshold

# Real question strings from Congress.gov House votes and senate.gov vote files/menus.
CASES = [
    # (chamber, question, is_rule, expected class, majority_requirement, expected threshold)
    ("house", "On Ordering the Previous Question", True, "rule", None, 0.5),
    ("house", "On Agreeing to the Resolution", True, "rule", None, 0.5),  # rule H.Res.
    ("house", "On Agreeing to the Resolution", False, "passage", None, 0.5),  # non-rule H.Res.
    ("house", "On Motion to Suspend the Rules and Pass", False, "suspension", None, 0.6667),
    ("house", "On Motion to Suspend the Rules and Pass, as Amended", False, "suspension", None, 0.6667),
    ("house", "On Motion to Suspend the Rules and Concur in the Senate Amendment", False, "suspension", None, 0.6667),
    ("house", "On Passage", False, "passage", None, 0.5),
    ("house", "On Agreeing to the Amendment", False, "amendment", None, 0.5),
    ("house", "On Motion to Recommit", False, "recommit", None, 0.5),
    ("house", "On Motion to Concur in the Senate Amendment", False, "final", None, 0.5),
    ("house", "On Agreeing to the Conference Report", False, "final", None, 0.5),
    ("house", "On Overriding the Veto", False, "veto_override", None, 0.6667),
    ("house", "On Motion to Table", False, "table", None, 0.5),
    ("house", "On Approving the Journal", False, "housekeeping", None, 0.5),
    ("house", "On Motion to Adjourn", False, "housekeeping", None, 0.5),
    ("house", "Election of the Speaker", False, "speaker", None, 0.5),
    ("senate", "On Passage of the Bill", False, "passage", "1/2", 0.5),
    ("senate", "On the Cloture Motion", False, "cloture", "3/5", 0.6),
    ("senate", "On the Cloture Motion", False, "cloture", "1/2", 0.5),  # cloture on a nomination
    ("senate", "On Cloture on the Motion to Proceed", False, "cloture", "3/5", 0.6),
    ("senate", "On the Nomination", False, "nomination", "1/2", 0.5),
    ("senate", "On the Amendment", False, "amendment", "1/2", 0.5),
    ("senate", "On the Motion to Table", False, "table", "1/2", 0.5),  # table an amendment
    ("senate", "On the Motion to Table the Amendment", False, "table", "1/2", 0.5),
    ("senate", "On the Motion to Proceed", False, "motion_to_proceed", "1/2", 0.5),
    ("senate", "On the Joint Resolution", False, "passage", "1/2", 0.5),
    ("senate", "On the Resolution", False, "passage", "1/2", 0.5),
    ("senate", "On the Concurrent Resolution", False, "passage", "1/2", 0.5),
    ("senate", "On the Resolution of Ratification", False, "ratification", "2/3", 0.6667),
    ("senate", "On the Decision of the Chair", False, "procedural", "1/2", 0.5),
    ("senate", "On the Motion to Waive", False, "procedural", "3/5", 0.6),
    ("senate", "On the Point of Order", False, "procedural", "1/2", 0.5),
    ("senate", "On the Motion to Discharge", False, "procedural", "1/2", 0.5),
    ("senate", "Guilty or Not Guilty", False, "impeachment", "2/3", 0.6667),
    ("senate", "On the Veto Message", False, "veto_override", "2/3", 0.6667),
    ("senate", "On the Motion to Concur in the House Amendment", False, "final", "1/2", 0.5),
]


@pytest.mark.parametrize("chamber,question,is_rule,expected,maj,thr", CASES)
def test_classification_and_threshold(chamber, question, is_rule, expected, maj, thr):
    cls = classify(chamber, question, is_rule=is_rule)
    assert cls == expected
    assert threshold(chamber, cls, maj) == thr


def test_unknown_question_is_other_and_logged(caplog, monkeypatch):
    monkeypatch.setattr("tracker.classify._warned", set())
    assert classify("senate", "On the Motion") == "other"
    assert "unclassified senate vote question" in caplog.text


def test_chamber_specific_rules_do_not_leak():
    # "Suspend the Rules" is a House procedure; "On the Nomination" is Senate-only.
    assert classify("senate", "On the Motion to Suspend the Rules") != "suspension"
    assert classify("house", "On the Nomination") == "other"


def test_concurrent_is_not_concur():
    assert classify("senate", "On the Concurrent Resolution") == "passage"


@pytest.mark.parametrize("value,expected", [("1/2", 0.5), ("3/5", 0.6), ("2/3", 0.6667), ("", None), ("x", None)])
def test_majority_requirement(value, expected):
    assert parse_majority_requirement(value) == expected

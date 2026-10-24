"""Vote classification and thresholds. Pure functions; first matching rule wins.

Rules match the published question text case-insensitively. Order matters: cloture and
motions to table are checked before the nomination, passage and amendment rules they
would otherwise fall into, and suspensions before "Concur" so a suspension motion to
concur keeps its two-thirds threshold.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from fractions import Fraction

log = logging.getLogger(__name__)

BOTH = ("house", "senate")
HOUSE = ("house",)
SENATE = ("senate",)


@dataclass(frozen=True)
class Rule:
    chambers: tuple[str, ...]
    pattern: re.Pattern[str]
    vote_class: str


def _r(chambers: tuple[str, ...], vote_class: str, *phrases: str) -> Rule:
    alts = "|".join(rf"\b{p}\b" for p in phrases)
    return Rule(chambers, re.compile(alts, re.IGNORECASE), vote_class)


HOUSE_RESOLUTION = "_house_resolution"

RULES: list[Rule] = [
    _r(BOTH, "veto_override", "overriding the veto", "veto message"),
    _r(SENATE, "impeachment", "guilty or not guilty"),
    _r(HOUSE, "speaker", "election of the speaker"),
    _r(SENATE, "cloture", "cloture"),
    _r(BOTH, "table", "table"),
    _r(HOUSE, "suspension", "suspend the rules"),
    _r(BOTH, "final", "conference report", "concur"),
    _r(BOTH, "passage", "on passage"),
    _r(SENATE, "ratification", "resolution of ratification"),
    _r(SENATE, "passage", "on the joint resolution", "on the resolution", "on the concurrent resolution"),
    # Resolved in classify(): 'rule' when the H.Res. is a rule, otherwise 'passage'.
    _r(HOUSE, HOUSE_RESOLUTION, "agreeing to the resolution"),
    _r(SENATE, "nomination", "on the nomination"),
    _r(BOTH, "amendment", "amendment"),
    _r(HOUSE, "rule", "ordering the previous question"),
    _r(SENATE, "motion_to_proceed", "motion to proceed"),
    _r(SENATE, "procedural", "waive", "point of order", "decision of the chair", "motion to discharge",
       "motion to reconsider", "motion to instruct", "motion to recommit"),
    _r(HOUSE, "recommit", "recommit"),
    _r(BOTH, "housekeeping", "adjourn", "journal", "quorum", "call of the house"),
]


def classify(chamber: str, question: str, *, is_rule: bool = False) -> str:
    """Return the vote_class for a question. `is_rule` says whether the vote's H.Res. is a rule."""
    q = " ".join((question or "").split())
    for rule in RULES:
        if chamber in rule.chambers and rule.pattern.search(q):
            if rule.vote_class == HOUSE_RESOLUTION:
                return "rule" if is_rule else "passage"
            return rule.vote_class
    log.warning("unclassified %s vote question %r -> other", chamber, question)
    return "other"


def parse_majority_requirement(value: str | None) -> float | None:
    """Senate majority_requirement: '1/2' -> 0.5, '3/5' -> 0.6, '2/3' -> 0.6667."""
    if not value:
        return None
    try:
        return round(float(Fraction(value.strip())), 4)
    except (ValueError, ZeroDivisionError):
        return None


def threshold(chamber: str, vote_class: str, majority_requirement: str | None = None) -> float:
    """Share of yea + nay votes needed to win."""
    if chamber == "senate":
        parsed = parse_majority_requirement(majority_requirement)
        if parsed is not None:
            return parsed
        return 0.6667 if vote_class in ("veto_override", "impeachment", "ratification") else 0.5
    return 0.6667 if vote_class in ("suspension", "veto_override") else 0.5

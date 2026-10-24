"""unitedstates/congress-legislators crosswalk: Bioguide <-> LIS IDs, chamber, party and caucus."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .congress_gov import Fetched
from .http import HttpClient

log = logging.getLogger(__name__)

PARTY_CODES = {"Democrat": "D", "Republican": "R", "Independent": "I", "Libertarian": "L"}


def party_code(name: str | None) -> str:
    if not name:
        return "?"
    return PARTY_CODES.get(name, name[:1].upper())


class CrosswalkError(Exception):
    pass


@dataclass(frozen=True)
class Affiliation:
    start: str
    end: str
    party: str  # code, e.g. 'D'
    caucus: str  # code; equals party unless the term says otherwise


@dataclass
class Person:
    bioguide: str
    lis: str | None
    name: str
    last_name: str
    chamber: str  # current chamber: 'house' | 'senate'
    state: str
    district: str | None
    affiliations: list[Affiliation] = field(default_factory=list)  # all terms, oldest first
    leadership_roles: list[dict[str, Any]] = field(default_factory=list)

    @property
    def party(self) -> str:
        return self.affiliations[-1].party if self.affiliations else "?"

    @property
    def caucus(self) -> str:
        return self.affiliations[-1].caucus if self.affiliations else "?"

    def affiliation_on(self, date: str) -> Affiliation | None:
        """The party affiliation in effect on an ISO date (YYYY-MM-DD...)."""
        day = date[:10]
        for a in reversed(self.affiliations):
            if a.start <= day <= a.end:
                return a
        return None

    def is_speaker_on(self, date: str) -> bool:
        day = date[:10]
        for r in self.leadership_roles:
            if r.get("title") == "Speaker of the House" and r.get("start", "") <= day <= r.get("end", "9999"):
                return True
        return False


def _affiliations(term: dict[str, Any]) -> list[Affiliation]:
    """Expand a term into affiliation periods, honoring mid-term party switches."""
    periods = term.get("party_affiliations") or [
        {"start": term["start"], "end": term["end"], "party": term.get("party"), "caucus": term.get("caucus")}
    ]
    out = []
    for p in periods:
        party = party_code(p.get("party"))
        caucus = party_code(p.get("caucus")) if p.get("caucus") else party
        out.append(Affiliation(p["start"], p["end"], party, caucus))
    return out


def parse_person(entry: dict[str, Any]) -> Person | None:
    ids = entry.get("id", {})
    bioguide = ids.get("bioguide")
    terms = entry.get("terms") or []
    if not bioguide or not terms:
        return None
    current = terms[-1]
    name = entry.get("name", {})
    full = name.get("official_full") or " ".join(p for p in (name.get("first"), name.get("last")) if p)
    affiliations = [a for t in terms for a in _affiliations(t)]
    district = current.get("district")
    return Person(
        bioguide=bioguide,
        lis=ids.get("lis"),
        name=full,
        last_name=name.get("last", full.split()[-1]),
        chamber="senate" if current.get("type") == "sen" else "house",
        state=current.get("state", ""),
        district=None if current.get("type") == "sen" or district is None else str(district),
        affiliations=affiliations,
        leadership_roles=list(entry.get("leadership_roles") or []),
    )


class Crosswalk:
    """Current members, plus (optionally) former members so votes cast before someone left
    office still map to a Bioguide ID. Only current members can be tracked."""

    def __init__(self, current: list[Person], former: list[Person] | None = None):
        self.current = {p.bioguide: p for p in current}
        everyone = list(former or []) + list(current)  # current wins on conflicts
        self.by_bioguide = {p.bioguide: p for p in everyone}
        self.by_lis = {p.lis: p for p in everyone if p.lis}

    @classmethod
    def from_json(cls, current_text: str, historical_text: str | None = None, since: str = "2000-01-01") -> "Crosswalk":
        current = [p for p in (parse_person(e) for e in json.loads(current_text)) if p]
        former: list[Person] = []
        if historical_text:
            for e in json.loads(historical_text):
                terms = e.get("terms") or []
                if terms and terms[-1].get("end", "") >= since:
                    p = parse_person(e)
                    if p:
                        former.append(p)
        return cls(current, former)

    def resolve(self, bioguide: str) -> Person:
        person = self.current.get(bioguide.upper())
        if person is None:
            raise CrosswalkError(f"Bioguide ID {bioguide} is not a current member in the crosswalk")
        if person.chamber == "senate" and not person.lis:
            raise CrosswalkError(f"Senator {bioguide} has no LIS ID in the crosswalk")
        return person

    def from_lis(self, lis: str) -> Person | None:
        return self.by_lis.get(lis)

    def caucus_on(self, bioguide: str, date: str, fallback_party: str) -> str:
        """Caucus on a date from the crosswalk, falling back to the party published with the vote."""
        person = self.by_bioguide.get(bioguide)
        aff = person.affiliation_on(date) if person else None
        if aff is None:
            return fallback_party
        # The vote payload's party is authoritative; only trust the crosswalk caucus when they agree.
        return aff.caucus if aff.party == fallback_party else fallback_party


def fetch_crosswalk(http: HttpClient, url: str) -> Fetched:
    return Fetched(url=url, text=http.get(url).text)

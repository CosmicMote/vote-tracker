"""State shared by the normalizers during one run."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .. import db
from ..sources.legislators import Crosswalk, Person
from .common import upsert_legislator_from_person

VOTE_EVENT_COLUMNS = (
    "chamber", "session", "roll_number", "vote_date", "question", "vote_title", "vote_class",
    "threshold", "result", "bill_id", "amendment_ref", "nomination_ref", "tie_breaker",
    "yea", "nay", "present", "not_voting", "source_url",
)


@dataclass
class NormalizeContext:
    conn: sqlite3.Connection
    crosswalk: Crosswalk | None
    tracked: set[str] = field(default_factory=set)
    stats: Counter = field(default_factory=Counter)
    unmapped_lis: Counter = field(default_factory=Counter)
    _seen_people: dict[str, int] = field(default_factory=dict)

    def raw(self, source: str, kind: str, external_id: str) -> sqlite3.Row | None:
        return db.get_raw(self.conn, source, kind, external_id)

    def legislator_from_person(self, person: Person) -> int:
        """Upsert a crosswalk person once per run."""
        if person.bioguide not in self._seen_people:
            self._seen_people[person.bioguide] = upsert_legislator_from_person(
                self.conn, person, tracked=person.bioguide in self.tracked
            )
        return self._seen_people[person.bioguide]

    def upsert_vote_event(self, **values: Any) -> int:
        cols = ", ".join(VOTE_EVENT_COLUMNS)
        marks = ", ".join("?" for _ in VOTE_EVENT_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in VOTE_EVENT_COLUMNS if c not in ("chamber", "session", "roll_number"))
        self.conn.execute(
            f"INSERT INTO vote_event (jurisdiction, {cols}) VALUES ('us', {marks})"
            f" ON CONFLICT(jurisdiction, chamber, session, roll_number) DO UPDATE SET {updates}",
            tuple(values[c] for c in VOTE_EVENT_COLUMNS),
        )
        row = self.conn.execute(
            "SELECT id FROM vote_event WHERE jurisdiction='us' AND chamber=? AND session=? AND roll_number=?",
            (values["chamber"], values["session"], values["roll_number"]),
        ).fetchone()
        return int(row["id"])

    def replace_member_votes(self, vote_id: int, rows: list[tuple]) -> None:
        self.conn.execute("DELETE FROM member_vote WHERE vote_event_id=?", (vote_id,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO member_vote"
            " (vote_event_id, legislator_id, position, position_raw, party_at_vote, caucus_at_vote)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

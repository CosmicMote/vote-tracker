"""Stage 5: build the weekly digest HTML from selected findings, summaries and vote context.

Everything about how a member voted is assembled here from data, by template. No LLM text
appears outside the bill summary bullets.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape

from . import analyze
from .analyze import Period
from .config import Settings
from .normalize.common import bill_display
from .sources.senate_xml import vote_page_url
from .summarize import summary_view

CLASS_PHRASES = {
    "rule": "Procedural vote to begin debate",
    "suspension": "Fast-track passage vote",
    "recommit": "Motion to send the bill back to committee",
    "cloture": "Vote to end debate",
    "motion_to_proceed": "Vote to begin consideration",
    "nomination": "Confirmation vote",
    "passage": "Final passage vote",
    "final": "Vote on the final version",
    "veto_override": "Vote to override the veto",
    "impeachment": "Impeachment verdict",
    "speaker": "Election of the Speaker",
    "ratification": "Treaty ratification vote",
    "amendment": "Amendment vote",
    "procedural": "Procedural vote",
    "table": "Motion to set aside",
    "housekeeping": "Housekeeping vote",
    "other": "Vote",
}

POSITION_LABELS = {"yea": "Yea", "nay": "Nay", "present": "Present", "not_voting": "Not Voting"}

FAILED_WORDS = ("not sustained", "rejected", "failed", "not agreed", "defeated", "not confirmed", "not passed")
PASSED_WORDS = ("passed", "agreed", "confirmed", "sustained", "adopted", "elected")

NOMINATION_PREFIXES = ("Confirmation:", "Motion to Invoke Cloture:", "Cloture Motion:", "Motion to Proceed:")


def plural(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"


def fmt_date(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def local_dt(iso_utc: str, tz: str) -> datetime:
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))


def caucus_name(caucus: str) -> str:
    return {"R": "Republican", "D": "Democratic caucus", "I": "independent"}.get(caucus, f"{caucus} caucus")


def member_title(chamber: str) -> str:
    return "Rep." if chamber == "house" else "Sen."


def threshold_note(threshold: float) -> str:
    if abs(threshold - 0.6) < 1e-3:
        return " (60 votes needed)"
    if abs(threshold - 0.6667) < 1e-3:
        return " (two-thirds needed)"
    return ""


def result_word(result: str | None, vote_class: str) -> str:
    r = (result or "").lower()
    if any(w in r for w in FAILED_WORDS):
        return "Rejected" if vote_class == "nomination" else "Failed"
    if any(w in r for w in PASSED_WORDS):
        return "Confirmed" if vote_class == "nomination" else "Passed"
    return result or "Result not published"


def nomination_description(vote: sqlite3.Row) -> str:
    title = vote["vote_title"] or vote["nomination_ref"] or "a nomination"
    for prefix in NOMINATION_PREFIXES:
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return title


def vote_subject(conn: sqlite3.Connection, vote: sqlite3.Row) -> str | None:
    if vote["nomination_ref"]:
        return nomination_description(vote)
    if vote["bill_id"] is None:
        return None
    b = conn.execute("SELECT bill_type, number FROM bill WHERE id=?", (vote["bill_id"],)).fetchone()
    number = bill_display(b["bill_type"], b["number"])
    if vote["vote_class"] == "rule":
        under = [x for x in analyze.underlying_bills(conn, vote["bill_id"]) if x != vote["bill_id"]]
        if under:
            names = []
            for bid in under:
                u = conn.execute("SELECT bill_type, number FROM bill WHERE id=?", (bid,)).fetchone()
                names.append(bill_display(u["bill_type"], u["number"]))
            return f"the rule ({number}) for " + " and ".join(names)
    if vote["vote_class"] in ("amendment", "table") and vote["amendment_ref"]:
        return f"{vote['amendment_ref']} to {number}"
    return number


def vote_outcome_line(conn: sqlite3.Connection, vote: sqlite3.Row) -> str:
    """'{class_phrase} on {subject}{threshold_note}. {result} {yea}–{nay}{tie_breaker_note}.'"""
    phrase = CLASS_PHRASES.get(vote["vote_class"], "Vote")
    subject = vote_subject(conn, vote)
    first = f"{phrase} on {subject}" if subject and vote["vote_class"] != "speaker" else phrase
    tie = ", with the Vice President breaking the tie" if vote["tie_breaker"] else ""
    return (
        f"{first}{threshold_note(vote['threshold'])}. "
        f"{result_word(vote['result'], vote['vote_class'])} {vote['yea']}–{vote['nay']}{tie}."
    )


def vote_context(conn: sqlite3.Connection, vote: sqlite3.Row, member: sqlite3.Row, detail: dict) -> list[str]:
    """The two-sentence vote context, built only from data."""
    line1 = vote_outcome_line(conn, vote)
    position = detail["position"]
    label = POSITION_LABELS[position] if detail["position_raw"] in ("Aye", "No") else detail["position_raw"]
    who = f"{member_title(member['chamber'])} {member['name']}"
    cp = analyze.get_caucus_position(conn, vote["id"], detail["caucus"])
    cname = caucus_name(detail["caucus"])
    if cp is None or cp.total == 0:
        line2 = f"{who} voted {label}."
    elif position in ("yea", "nay"):
        same = cp.yea if position == "yea" else cp.nay
        other = cp.total - same
        if same >= other:
            line2 = f"{who} voted {label}, with {same} of {cp.total} {cname} members."
        else:
            line2 = f"{who} voted {label}, against {other} of {cp.total} {cname} members."
    else:
        line2 = f"{who} voted {label}; {cname} members split {cp.yea}–{cp.nay}."
    return [line1, line2]


def roll_call_url(vote: sqlite3.Row) -> str | None:
    congress, session = (int(x) for x in vote["session"].split("-"))
    if vote["chamber"] == "senate":
        return vote_page_url(congress, session, vote["roll_number"])
    year = local_dt(vote["vote_date"], "America/New_York").year
    return f"https://clerk.house.gov/Votes/{year}{vote['roll_number']}"


class DigestBuilder:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, period: Period):
        self.conn = conn
        self.settings = settings
        self.period = period
        self.tz = settings.digest.timezone
        self.shown: dict[int, str] = {}  # bill_id -> anchor of its first appearance

    def bill_block(self, bill_id: int) -> dict:
        view = summary_view(self.conn, bill_id, self.settings)
        anchor = f"bill-{bill_id}"
        if bill_id in self.shown:
            return {**view, "see_above": self.shown[bill_id], "anchor": None}
        self.shown[bill_id] = anchor
        return {**view, "see_above": None, "anchor": anchor}

    def vote_line(self, vote: sqlite3.Row, member: sqlite3.Row | None, detail: dict | None) -> dict:
        if member is None or detail is None:
            context = [vote_outcome_line(self.conn, vote)]
        else:
            context = vote_context(self.conn, vote, member, detail)
        return {
            "context": context,
            "date": fmt_date(local_dt(vote["vote_date"], self.tz)),
            "roll_label": f"Roll call {vote['roll_number']}",
            "roll_url": roll_call_url(vote),
            "chamber": vote["chamber"],
        }

    def item(self, votes: list[tuple[sqlite3.Row, dict | None]], member: sqlite3.Row | None) -> dict:
        """One digest item: a bill (or nomination) with every selected vote on it, oldest first."""
        votes = sorted(votes, key=lambda x: (x[0]["vote_date"], x[0]["roll_number"]))
        first = votes[0][0]
        bills = []
        if first["bill_id"] is not None:
            bills = [self.bill_block(b) for b in analyze.underlying_bills(self.conn, first["bill_id"])]
        if bills:
            heading = bills[0]["plain_title"]
            number = " / ".join(b["number"] for b in bills)
        elif first["nomination_ref"]:
            # Voteview drops the part suffix (PN11-7 becomes PN117), so its numbers are not shown.
            ref = None if self.settings.senate.source == "voteview" else first["nomination_ref"]
            heading, number = nomination_description(first), ref
        else:
            heading, number = first["vote_title"] or first["question"], None
        if bills and heading == bills[0]["number"]:
            heading, number = number, None  # no title published: lead with the number(s)
        elif heading == number:
            number = None
        return {
            "heading": heading,
            "number": number,
            "bills": bills,
            "votes": [self.vote_line(v, member, d) for v, d in votes],
        }

    def findings(self, member: sqlite3.Row, kind: str, selected_only: bool = True) -> list[sqlite3.Row]:
        sql = (
            "SELECT f.*, v.vote_date FROM finding f LEFT JOIN vote_event v ON v.id = f.vote_event_id"
            " WHERE f.legislator_id=? AND f.period_start=? AND f.period_end=? AND f.kind=?"
        )
        if selected_only:
            sql += " AND f.selected=1"
        return self.conn.execute(
            sql + " ORDER BY f.score DESC, v.vote_date, f.id",
            (member["id"], self.period.start.isoformat(), self.period.end.isoformat(), kind),
        ).fetchall()

    def vote_row(self, vote_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM vote_event WHERE id=?", (vote_id,)).fetchone()

    def member_section(self, member: sqlite3.Row) -> dict:
        stats = analyze.member_stats(self.conn, member, self.period, self.settings.scoring)
        breaks = [
            self.item([(self.vote_row(f["vote_event_id"]), json.loads(f["detail_json"]))], member)
            for f in self.findings(member, "party_break")
        ]
        groups: dict[str, list] = defaultdict(list)
        order: list[str] = []
        for f in self.findings(member, "vote"):
            d = json.loads(f["detail_json"])
            if d["group"] not in groups:
                order.append(d["group"])
            groups[d["group"]].append((self.vote_row(f["vote_event_id"]), d))
        notable = [self.item(groups[g], member) for g in order]

        all_missed = self.findings(member, "missed_vote", selected_only=False)
        speaker = any(json.loads(f["detail_json"]).get("speaker") for f in all_missed)
        missed_listed = [
            self.item([(self.vote_row(f["vote_event_id"]), json.loads(f["detail_json"]))], member)
            for f in all_missed
            if f["selected"]
        ]
        sponsorships = []
        for kind, label in (("sponsored", "Introduced"), ("cosponsored", "Cosponsored")):
            for f in sorted(self.findings(member, kind), key=lambda r: (json.loads(r["detail_json"])["date"], r["bill_id"])):
                view = summary_view(self.conn, f["bill_id"], self.settings)
                d = json.loads(f["detail_json"])
                sponsorships.append(
                    {
                        "role": label,
                        "number": view["number"],
                        "plain_title": view["plain_title"],
                        "date": fmt_date(datetime.fromisoformat(d["date"]).date()),
                        "url": view["url"],
                    }
                )
        district = f"-{member['district']}" if member["district"] else ""
        party = self.conn.execute(
            "SELECT party FROM party_membership WHERE legislator_id=? ORDER BY start_date DESC LIMIT 1", (member["id"],)
        ).fetchone()
        return {
            "anchor": f"member-{member['bioguide_id']}",
            "title": member_title(member["chamber"]),
            "name": member["name"],
            "label": f"{party['party'] if party else '?'}-{member['state']}{district}",
            "chamber": member["chamber"],
            "stats": stats,
            "breaks": breaks,
            "notable": notable,
            "missed_count": stats["missed"],
            "missed_listed": missed_listed,
            "is_speaker": speaker,
            "sponsorships": sponsorships,
        }

    def splits(self) -> list[dict]:
        out = []
        for s in analyze.delegation_splits(self.conn, self.period):
            v = s["vote"]
            item = self.item([(v, None)], None)
            item["positions"] = [f"{member_title(m['chamber'])} {m['name']}: {pos}" for m, pos in s["positions"]]
            out.append(item)
        return out

    def as_of(self) -> list[dict]:
        labels = {
            "congress_gov": "Congress.gov (House votes, bills, members)",
            "senate_xml": "senate.gov (Senate votes)",
            "voteview": "Voteview (Senate votes)",
            "legislators": "congress-legislators (ID crosswalk)",
        }
        rows = self.conn.execute(
            "SELECT source, MAX(fetched_at) AS t FROM raw_record GROUP BY source ORDER BY source"
        ).fetchall()
        return [
            {"source": labels.get(r["source"], r["source"]), "at": f"{local_dt(r['t'], self.tz):%b %-d, %Y %-I:%M %p %Z}"}
            for r in rows
        ]

    def stale_warnings(self) -> list[str]:
        out = []
        for chamber, label in (("house", "House"), ("senate", "Senate")):
            attempt = self.conn.execute(
                "SELECT cursor, last_run FROM sync_state WHERE source=?", (f"ingest_attempt:{chamber}",)
            ).fetchone()
            if attempt and attempt["cursor"] == "failed":
                ok = self.conn.execute(
                    "SELECT last_run FROM sync_state WHERE source=?", (f"ingest_ok:{chamber}",)
                ).fetchone()
                since = f"last successful update {local_dt(ok['last_run'], self.tz):%b %-d, %Y}" if ok else "never updated"
                out.append(f"{label} data may be stale: the latest {label} ingest failed ({since}).")
        return out

    def build(self) -> dict:
        splits = self.splits()
        members = [self.member_section(m) for m in analyze.tracked_members(self.conn)]
        n_notable = sum(len(m["notable"]) for m in members)
        n_breaks = sum(len(m["breaks"]) for m in members)
        week = fmt_date(self.period.start)
        return {
            "subject": (
                f"Your delegation — week of {week}: {n_notable} notable {plural(n_notable, 'vote')}, "
                f"{n_breaks} {plural(n_breaks, 'break')} from party"
            ),
            "week_label": f"{fmt_date(self.period.start)} – {fmt_date(self.period.end)}",
            "splits": splits,
            "members": members,
            "as_of": self.as_of(),
            "stale": self.stale_warnings(),
            "prompt_version": self.settings.llm.prompt_version,
            "senate_source": self.settings.senate.source,
        }


def environment() -> Environment:
    return Environment(
        loader=PackageLoader("tracker", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_digest(conn: sqlite3.Connection, settings: Settings, period: Period) -> tuple[str, str]:
    """Returns (subject, html)."""
    ctx = DigestBuilder(conn, settings, period).build()
    html = environment().get_template("digest.html.j2").render(**ctx)
    return ctx["subject"], html


def digest_path(settings: Settings, period: Period) -> Path:
    return settings.storage.output_dir / f"{period.start.isoformat()}.html"


def write_digest(conn: sqlite3.Connection, settings: Settings, period: Period) -> tuple[str, Path]:
    subject, html = render_digest(conn, settings, period)
    path = digest_path(settings, period)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return subject, path

"""Shared normalize helpers: bill references, rule links, HTML stripping, legislator and bill upserts."""

from __future__ import annotations

import html
import re
import sqlite3
from html.parser import HTMLParser

from ..sources.legislators import Person

BILL_TYPES = ("hr", "s", "hres", "sres", "hjres", "sjres", "hconres", "sconres")

# Congress.gov web paths per bill type
BILL_URL_SLUG = {
    "hr": "house-bill",
    "s": "senate-bill",
    "hres": "house-resolution",
    "sres": "senate-resolution",
    "hjres": "house-joint-resolution",
    "sjres": "senate-joint-resolution",
    "hconres": "house-concurrent-resolution",
    "sconres": "senate-concurrent-resolution",
}

BILL_DISPLAY = {
    "hr": "H.R.",
    "s": "S.",
    "hres": "H.Res.",
    "sres": "S.Res.",
    "hjres": "H.J.Res.",
    "sjres": "S.J.Res.",
    "hconres": "H.Con.Res.",
    "sconres": "S.Con.Res.",
}

# A citation in running text, e.g. "H.R. 3838", "S. 5", "H. J. Res. 20", "H.Con.Res. 14".
# Longest forms first; the lookbehind keeps "U.S. 5" from reading as "S. 5".
_D = r"\.?\s*"
_REF_RE = re.compile(
    r"(?<![A-Za-z.])("
    rf"H{_D}Con{_D}Res|S{_D}Con{_D}Res|H{_D}J{_D}Res|S{_D}J{_D}Res|H{_D}Res|S{_D}Res|H{_D}R|S"
    r")\.?\s*(\d+)\b",
    re.IGNORECASE,
)


def canonical_type(raw: str) -> str | None:
    t = re.sub(r"[\s.]", "", raw).lower()
    return t if t in BILL_TYPES else None


def parse_bill_ref(text: str | None) -> tuple[str, str] | None:
    """Parse one reference such as 'H.R. 1234', 'S.J.Res. 7' or Congress.gov's 'HJRES' + number."""
    if not text:
        return None
    m = _REF_RE.fullmatch(text.strip()) or _REF_RE.match(text.strip())
    if not m:
        return None
    t = canonical_type(m.group(1))
    return (t, m.group(2)) if t else None


def find_bill_refs(text: str | None) -> list[tuple[str, str]]:
    """Every bill citation in a block of text, in order, without duplicates."""
    seen: list[tuple[str, str]] = []
    for m in _REF_RE.finditer(text or ""):
        t = canonical_type(m.group(1))
        if t and (t, m.group(2)) not in seen:
            seen.append((t, m.group(2)))
    return seen


RULE_PREFIX = "providing for consideration of"


def is_rule_title(bill_type: str, title: str | None) -> bool:
    return bill_type == "hres" and bool(title) and title.strip().lower().startswith(RULE_PREFIX)


# Rules name what they bring up as "the bill (H.R. 3838)", "the joint resolution (H.J. Res. 20)", ...
_RULE_TARGET_RE = re.compile(
    r"\bthe (?:bill|joint resolution|concurrent resolution|resolution)\s*\(([^)]+)\)", re.IGNORECASE
)


def rule_targets(rule_number: str, title: str) -> list[tuple[str, str]]:
    """Bills a House rule brings to the floor, parsed from its title.

    Only parenthesized measure citations count, so background references such as
    "pursuant to title II of H. Con. Res. 14" are not mistaken for underlying bills.
    """
    cited = " ".join(m.group(1) for m in _RULE_TARGET_RE.finditer(title))
    refs = find_bill_refs(cited) if cited else find_bill_refs(title)
    return [ref for ref in refs if ref != ("hres", rule_number)]


def bill_display(bill_type: str, number: str) -> str:
    return f"{BILL_DISPLAY.get(bill_type, bill_type.upper())} {number}"


def bill_web_url(congress: str | int, bill_type: str, number: str) -> str:
    return f"https://www.congress.gov/bill/{congress}th-congress/{BILL_URL_SLUG[bill_type]}/{number}"


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(text: str | None) -> str:
    """HTML to plain text, keeping paragraph breaks and collapsing other whitespace."""
    if not text:
        return ""
    p = _TextExtractor()
    p.feed(text)
    p.close()
    raw = html.unescape("".join(p.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# --- upserts ----------------------------------------------------------------


def get_or_create_bill(conn: sqlite3.Connection, session: str, bill_type: str, number: str) -> int:
    """Return the bill id, creating a stub row (title unknown) if it does not exist yet."""
    bill_type = bill_type.lower()
    number = str(int(number)) if str(number).isdigit() else str(number)
    row = conn.execute(
        "SELECT id FROM bill WHERE jurisdiction='us' AND session=? AND bill_type=? AND number=?",
        (session, bill_type, number),
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO bill (jurisdiction, session, bill_type, number, url) VALUES ('us', ?, ?, ?, ?)",
        (session, bill_type, number, bill_web_url(session, bill_type, number) if bill_type in BILL_URL_SLUG else None),
    )
    return int(cur.lastrowid)


def upsert_legislator_from_person(conn: sqlite3.Connection, person: Person, tracked: bool | None = None) -> int:
    """Insert or refresh a legislator from the crosswalk, and rebuild their party history."""
    row = conn.execute("SELECT id, tracked FROM legislator WHERE bioguide_id=?", (person.bioguide,)).fetchone()
    if row is None and person.lis:
        # Never let a stale LIS assignment block an insert.
        conn.execute("UPDATE legislator SET lis_id=NULL WHERE lis_id=? AND bioguide_id IS NOT ?", (person.lis, person.bioguide))
    if row is None:
        cur = conn.execute(
            "INSERT INTO legislator (bioguide_id, lis_id, name, level, chamber, state, district, tracked)"
            " VALUES (?, ?, ?, 'federal', ?, ?, ?, ?)",
            (person.bioguide, person.lis, person.name, person.chamber, person.state, person.district, int(bool(tracked))),
        )
        leg_id = int(cur.lastrowid)
    else:
        leg_id = int(row["id"])
        conn.execute(
            "UPDATE legislator SET lis_id=COALESCE(?, lis_id), name=?, chamber=?, state=?, district=?, tracked=?"
            " WHERE id=?",
            (
                person.lis,
                person.name,
                person.chamber,
                person.state,
                person.district,
                int(row["tracked"]) if tracked is None else int(tracked),
                leg_id,
            ),
        )
    conn.execute("DELETE FROM party_membership WHERE legislator_id=?", (leg_id,))
    conn.executemany(
        "INSERT INTO party_membership (legislator_id, party, caucus, start_date, end_date) VALUES (?, ?, ?, ?, ?)",
        [(leg_id, a.party, a.caucus, a.start, a.end) for a in person.affiliations],
    )
    return leg_id


def upsert_legislator_minimal(
    conn: sqlite3.Connection,
    bioguide: str,
    name: str,
    chamber: str,
    state: str | None,
    district: str | None = None,
) -> int:
    """Upsert a member seen in a vote but absent from the crosswalk. Never overwrites crosswalk data."""
    row = conn.execute("SELECT id FROM legislator WHERE bioguide_id=?", (bioguide,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO legislator (bioguide_id, name, level, chamber, state, district, tracked)"
        " VALUES (?, ?, 'federal', ?, ?, ?, 0)",
        (bioguide, name, chamber, state, district),
    )
    return int(cur.lastrowid)

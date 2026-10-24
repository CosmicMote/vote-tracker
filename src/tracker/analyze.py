"""Stage 3: caucus positions, findings, scoring and selection for one week. Deterministic."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import ScoringConfig, Settings
from .normalize import load_crosswalk

log = logging.getLogger(__name__)


# --- periods ----------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    start: date  # Monday, local
    end: date  # Sunday, local
    tz: str

    @property
    def start_utc(self) -> str:
        return _utc(datetime.combine(self.start, time.min, ZoneInfo(self.tz)))

    @property
    def end_utc(self) -> str:
        """Exclusive upper bound: the following Monday at local midnight."""
        return _utc(datetime.combine(self.end + timedelta(days=1), time.min, ZoneInfo(self.tz)))


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def week_containing(d: date, tz: str) -> Period:
    monday = d - timedelta(days=d.weekday())
    return Period(monday, monday + timedelta(days=6), tz)


def last_full_week(tz: str, now: datetime | None = None) -> Period:
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz)).date()
    return week_containing(today - timedelta(days=today.weekday() + 1), tz)


# --- caucus positions ---------------------------------------------------------


@dataclass(frozen=True)
class CaucusPosition:
    caucus: str
    yea: int
    nay: int
    majority: str | None
    cohesion: float | None

    @property
    def total(self) -> int:
        return self.yea + self.nay


def caucus_position(caucus: str, yea: int, nay: int) -> CaucusPosition:
    total = yea + nay
    majority = None if yea == nay else ("yea" if yea > nay else "nay")
    cohesion = max(yea, nay) / total if total else None
    return CaucusPosition(caucus, yea, nay, majority, cohesion)


def compute_caucus_positions(conn: sqlite3.Connection, vote_ids: list[int]) -> None:
    for vid in vote_ids:
        conn.execute("DELETE FROM caucus_position WHERE vote_event_id=?", (vid,))
        rows = conn.execute(
            "SELECT caucus_at_vote AS caucus, SUM(position='yea') AS yea, SUM(position='nay') AS nay"
            " FROM member_vote WHERE vote_event_id=? GROUP BY caucus_at_vote ORDER BY caucus_at_vote",
            (vid,),
        ).fetchall()
        for r in rows:
            cp = caucus_position(r["caucus"], int(r["yea"]), int(r["nay"]))
            conn.execute(
                "INSERT INTO caucus_position (vote_event_id, caucus, yea, nay, majority, cohesion) VALUES (?, ?, ?, ?, ?, ?)",
                (vid, cp.caucus, cp.yea, cp.nay, cp.majority, cp.cohesion),
            )


def get_caucus_position(conn: sqlite3.Connection, vote_id: int, caucus: str) -> CaucusPosition | None:
    r = conn.execute(
        "SELECT * FROM caucus_position WHERE vote_event_id=? AND caucus=?", (vote_id, caucus)
    ).fetchone()
    return CaucusPosition(r["caucus"], r["yea"], r["nay"], r["majority"], r["cohesion"]) if r else None


# --- scoring (pure) -------------------------------------------------------------


def yea_share(yea: int, nay: int) -> float:
    total = yea + nay
    return yea / total if total else 1.0


def closeness(yea: int, nay: int, threshold: float) -> float:
    """1.0 when far from the threshold, 2.0 when on the line."""
    distance = abs(yea_share(yea, nay) - threshold)
    return 1 + (1 - distance / max(threshold, 1 - threshold))


def is_party_break(position: str, cp: CaucusPosition | None, cfg: ScoringConfig) -> bool:
    if position not in ("yea", "nay") or cp is None or cp.majority is None:
        return False
    if cp.total < cfg.break_min_caucus_size or (cp.cohesion or 0) < cfg.break_min_cohesion:
        return False
    return position != cp.majority


def is_lopsided(yea: int, nay: int, cfg: ScoringConfig) -> bool:
    share = yea_share(yea, nay)
    return share > cfg.lopsided_share or share < 1 - cfg.lopsided_share


def score_vote(
    vote_class: str,
    threshold: float,
    yea: int,
    nay: int,
    *,
    party_break: bool,
    sponsored: bool,
    tie_breaker: bool,
    policy_area: str | None,
    cfg: ScoringConfig,
) -> float:
    score = cfg.base_weights.get(vote_class, cfg.base_weights["other"]) * closeness(yea, nay, threshold)
    if party_break:
        score += cfg.party_break_bonus
    if sponsored:
        score += cfg.sponsor_bonus
    if tie_breaker:
        score += cfg.tie_breaker_bonus
    if policy_area and policy_area in cfg.topic_boost:
        score += cfg.topic_boost[policy_area]
    return round(score, 4)


# --- vote context helpers ---------------------------------------------------------


def underlying_bills(conn: sqlite3.Connection, bill_id: int | None) -> list[int]:
    """For a House rule, the bills it brings up; otherwise the bill itself."""
    if bill_id is None:
        return []
    rows = conn.execute(
        "SELECT underlying_bill_id FROM rule_link WHERE rule_bill_id=? ORDER BY underlying_bill_id", (bill_id,)
    ).fetchall()
    return [r[0] for r in rows] or [bill_id]


def group_key(conn: sqlite3.Connection, vote: sqlite3.Row) -> str:
    """Votes on the same bill or nomination collapse into one digest item."""
    if vote["bill_id"] is not None:
        return f"bill:{underlying_bills(conn, vote['bill_id'])[0]}"
    if vote["nomination_ref"]:
        return f"nomination:{vote['session'].split('-')[0]}:{vote['nomination_ref']}"
    return f"vote:{vote['id']}"


def votes_in_period(conn: sqlite3.Connection, period: Period, chamber: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM vote_event WHERE vote_date >= ? AND vote_date < ?"
    args: list = [period.start_utc, period.end_utc]
    if chamber:
        sql += " AND chamber=?"
        args.append(chamber)
    return conn.execute(sql + " ORDER BY vote_date, chamber, roll_number", args).fetchall()


def sponsored_bill_ids(conn: sqlite3.Connection, legislator_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT bill_id FROM sponsorship WHERE legislator_id=? AND withdrawn IS NULL", (legislator_id,)
    ).fetchall()
    return {r[0] for r in rows}


NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def last_name(full: str) -> str:
    parts = [p for p in full.replace(",", " ").split() if p.lower() not in NAME_SUFFIXES]
    return parts[-1] if parts else full


def tracked_members(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Representative first, then senators alphabetically by last name."""
    rows = conn.execute("SELECT * FROM legislator WHERE tracked=1").fetchall()
    return sorted(rows, key=lambda r: (r["chamber"] == "senate", last_name(r["name"]).lower(), r["name"]))


# --- analysis ---------------------------------------------------------------------


@dataclass
class VoteEval:
    vote: sqlite3.Row
    position: str
    position_raw: str
    caucus: str
    cp: CaucusPosition | None
    party_break: bool
    sponsored: bool
    lopsided: bool
    score: float
    group: str

    def detail(self) -> dict:
        d = {
            "position": self.position,
            "position_raw": self.position_raw,
            "caucus": self.caucus,
            "caucus_majority": self.cp.majority if self.cp else None,
            "caucus_split": f"{self.cp.yea}-{self.cp.nay}" if self.cp else None,
            "cohesion": round(self.cp.cohesion, 4) if self.cp and self.cp.cohesion is not None else None,
            "party_break": self.party_break,
            "sponsored": self.sponsored,
            "lopsided": self.lopsided,
            "group": self.group,
        }
        return d


def evaluate_member_votes(
    conn: sqlite3.Connection, legislator: sqlite3.Row, period: Period, cfg: ScoringConfig
) -> list[VoteEval]:
    """Every vote in the member's own chamber during the period that they were seated for."""
    sponsored = sponsored_bill_ids(conn, legislator["id"])
    out = []
    for v in votes_in_period(conn, period, legislator["chamber"]):
        mv = conn.execute(
            "SELECT * FROM member_vote WHERE vote_event_id=? AND legislator_id=?", (v["id"], legislator["id"])
        ).fetchone()
        if mv is None:
            continue  # not seated for this vote (e.g. sworn in mid-period): not a missed vote
        cp = get_caucus_position(conn, v["id"], mv["caucus_at_vote"])
        bills = underlying_bills(conn, v["bill_id"])
        is_sponsor = any(b in sponsored for b in bills)
        brk = is_party_break(mv["position"], cp, cfg)
        policy = None
        if bills:
            row = conn.execute("SELECT policy_area FROM bill WHERE id=?", (bills[0],)).fetchone()
            policy = row["policy_area"] if row else None
        score = score_vote(
            v["vote_class"],
            v["threshold"],
            v["yea"] or 0,
            v["nay"] or 0,
            party_break=brk,
            sponsored=is_sponsor,
            tie_breaker=bool(v["tie_breaker"]),
            policy_area=policy,
            cfg=cfg,
        )
        out.append(
            VoteEval(
                vote=v,
                position=mv["position"],
                position_raw=mv["position_raw"],
                caucus=mv["caucus_at_vote"],
                cp=cp,
                party_break=brk,
                sponsored=is_sponsor,
                lopsided=is_lopsided(v["yea"] or 0, v["nay"] or 0, cfg),
                score=score,
                group=group_key(conn, v),
            )
        )
    return out


def select_notable(evals: list[VoteEval], top_n: int) -> set[int]:
    """Vote ids for the 'notable votes' section: top groups among votes that are not party breaks."""
    groups: dict[str, list[VoteEval]] = defaultdict(list)
    for e in evals:
        if e.position == "not_voting" or e.party_break:
            continue
        if e.vote["vote_class"] == "housekeeping":
            continue
        if e.lopsided and not e.sponsored:
            continue
        groups[e.group].append(e)
    ranked = sorted(
        groups.values(),
        key=lambda g: (-max(e.score for e in g), min(e.vote["vote_date"] for e in g), min(e.vote["id"] for e in g)),
    )
    return {e.vote["id"] for g in ranked[:top_n] for e in g}


def is_speaker(conn: sqlite3.Connection, legislator: sqlite3.Row, period: Period, congress: int) -> bool:
    if legislator["chamber"] != "house":
        return False
    crosswalk = load_crosswalk(conn, congress)
    person = crosswalk.by_bioguide.get(legislator["bioguide_id"]) if crosswalk else None
    return bool(person and person.is_speaker_on(period.end.isoformat()))


@dataclass
class AnalyzeResult:
    period: Period
    stats: Counter = field(default_factory=Counter)


def run_analyze(conn: sqlite3.Connection, settings: Settings, period: Period) -> AnalyzeResult:
    cfg = settings.scoring
    result = AnalyzeResult(period)
    votes = votes_in_period(conn, period)
    compute_caucus_positions(conn, [v["id"] for v in votes])
    result.stats["votes"] = len(votes)
    ps, pe = period.start.isoformat(), period.end.isoformat()

    for leg in tracked_members(conn):
        conn.execute(
            "DELETE FROM finding WHERE legislator_id=? AND period_start=? AND period_end=?", (leg["id"], ps, pe)
        )
        evals = evaluate_member_votes(conn, leg, period, cfg)
        notable = select_notable(evals, settings.digest.top_n)
        speaker = is_speaker(conn, leg, period, settings.congress.number)
        rows = []
        for e in evals:
            detail = e.detail()
            rows.append((leg["id"], ps, pe, "vote", e.vote["id"], e.vote["bill_id"], e.score,
                         int(e.vote["id"] in notable), json.dumps(detail, sort_keys=True)))
            if e.party_break:
                rows.append((leg["id"], ps, pe, "party_break", e.vote["id"], e.vote["bill_id"], e.score, 1,
                             json.dumps(detail, sort_keys=True)))
            if e.position == "not_voting":
                detail["speaker"] = speaker
                selected = int(not speaker and e.score >= settings.digest.missed_vote_min_score)
                rows.append((leg["id"], ps, pe, "missed_vote", e.vote["id"], e.vote["bill_id"], e.score, selected,
                             json.dumps(detail, sort_keys=True)))
        for s in conn.execute(
            "SELECT * FROM sponsorship WHERE legislator_id=? AND date >= ? AND date <= ? AND withdrawn IS NULL"
            " ORDER BY date, bill_id, role",
            (leg["id"], ps, pe),
        ):
            kind = "sponsored" if s["role"] == "sponsor" else "cosponsored"
            rows.append((leg["id"], ps, pe, kind, None, s["bill_id"], None, 1,
                         json.dumps({"date": s["date"], "withdrawn": s["withdrawn"]}, sort_keys=True)))
        conn.executemany(
            "INSERT INTO finding (legislator_id, period_start, period_end, kind, vote_event_id, bill_id, score,"
            " selected, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        kinds = Counter(r[3] for r in rows)
        result.stats[f"{leg['bioguide_id']}:votes"] = kinds["vote"]
        result.stats[f"{leg['bioguide_id']}:party_breaks"] = kinds["party_break"]
        result.stats[f"{leg['bioguide_id']}:missed"] = kinds["missed_vote"]
        result.stats[f"{leg['bioguide_id']}:notable"] = len(notable)
    conn.commit()
    return result


# --- read helpers for render and explain ------------------------------------------


def delegation_splits(conn: sqlite3.Connection, period: Period) -> list[dict]:
    """Senate votes where two tracked senators voted opposite ways (yea against nay)."""
    senators = [m for m in tracked_members(conn) if m["chamber"] == "senate"]
    if len(senators) < 2:
        return []
    a, b = senators[0], senators[1]
    out = []
    for v in votes_in_period(conn, period, "senate"):
        pos = {}
        for s in (a, b):
            r = conn.execute(
                "SELECT position, position_raw FROM member_vote WHERE vote_event_id=? AND legislator_id=?",
                (v["id"], s["id"]),
            ).fetchone()
            pos[s["id"]] = r
        pa, pb = pos[a["id"]], pos[b["id"]]
        if pa and pb and {pa["position"], pb["position"]} == {"yea", "nay"}:
            out.append({"vote": v, "positions": [(a, pa["position_raw"]), (b, pb["position_raw"])]})
    return out


def member_stats(conn: sqlite3.Connection, legislator: sqlite3.Row, period: Period, cfg: ScoringConfig) -> dict:
    """Votes cast, votes missed, and caucus agreement on contested votes."""
    rows = conn.execute(
        "SELECT mv.position, cp.majority, cp.cohesion FROM member_vote mv"
        " JOIN vote_event v ON v.id = mv.vote_event_id"
        " LEFT JOIN caucus_position cp ON cp.vote_event_id = v.id AND cp.caucus = mv.caucus_at_vote"
        " WHERE mv.legislator_id=? AND v.vote_date >= ? AND v.vote_date < ? AND v.chamber=?",
        (legislator["id"], period.start_utc, period.end_utc, legislator["chamber"]),
    ).fetchall()
    cast = sum(1 for r in rows if r["position"] in ("yea", "nay", "present"))
    missed = sum(1 for r in rows if r["position"] == "not_voting")
    present = sum(1 for r in rows if r["position"] == "present")
    contested = [
        r for r in rows
        if r["position"] in ("yea", "nay") and r["majority"] and r["cohesion"] is not None
        and r["cohesion"] < cfg.contested_cohesion
    ]
    agreed = sum(1 for r in contested if r["position"] == r["majority"])
    return {"cast": cast, "missed": missed, "present": present, "contested": len(contested), "agreed": agreed}


def explain_vote(conn: sqlite3.Connection, settings: Settings, chamber: str, session: str, roll: int) -> dict:
    v = conn.execute(
        "SELECT * FROM vote_event WHERE chamber=? AND session=? AND roll_number=?", (chamber, session, roll)
    ).fetchone()
    if v is None:
        raise LookupError(f"No {chamber} vote {session} #{roll} in the database")
    compute_caucus_positions(conn, [v["id"]])
    conn.commit()
    cps = conn.execute(
        "SELECT * FROM caucus_position WHERE vote_event_id=? ORDER BY caucus", (v["id"],)
    ).fetchall()
    out = {
        "vote": dict(v),
        "yea_share": round(yea_share(v["yea"] or 0, v["nay"] or 0), 4),
        "closeness": round(closeness(v["yea"] or 0, v["nay"] or 0, v["threshold"]), 4),
        "lopsided": is_lopsided(v["yea"] or 0, v["nay"] or 0, settings.scoring),
        "base_weight": settings.scoring.base_weights.get(v["vote_class"]),
        "caucus_positions": [dict(r) for r in cps],
        "members": [],
    }
    for leg in tracked_members(conn):
        if leg["chamber"] != chamber:
            continue
        mv = conn.execute(
            "SELECT * FROM member_vote WHERE vote_event_id=? AND legislator_id=?", (v["id"], leg["id"])
        ).fetchone()
        if mv is None:
            out["members"].append({"name": leg["name"], "position": None})
            continue
        cp = get_caucus_position(conn, v["id"], mv["caucus_at_vote"])
        bills = underlying_bills(conn, v["bill_id"])
        is_sponsor = any(b in sponsored_bill_ids(conn, leg["id"]) for b in bills)
        brk = is_party_break(mv["position"], cp, settings.scoring)
        policy = None
        if bills:
            policy = conn.execute("SELECT policy_area FROM bill WHERE id=?", (bills[0],)).fetchone()["policy_area"]
        out["members"].append(
            {
                "name": leg["name"],
                "position": mv["position_raw"],
                "caucus": mv["caucus_at_vote"],
                "party_break": brk,
                "sponsored": is_sponsor,
                "score": score_vote(
                    v["vote_class"], v["threshold"], v["yea"] or 0, v["nay"] or 0,
                    party_break=brk, sponsored=is_sponsor, tie_breaker=bool(v["tie_breaker"]),
                    policy_area=policy, cfg=settings.scoring,
                ),
            }
        )
    return out

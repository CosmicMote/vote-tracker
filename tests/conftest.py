from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tracker import db
from tracker.config import DigestConfig, LlmConfig, MembersConfig, SenateConfig, Settings, StorageConfig

FIXTURES = Path(__file__).parent / "fixtures"
TRACKED = ["D000197", "B001267", "H000273"]  # DeGette (CO-1), Bennet, Hickenlooper
FETCHED_AT = "2025-11-17T12:00:00Z"


def fixture_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The suite runs offline: any real socket connection is a test failure."""
    import socket

    def guard(*args, **kwargs):
        raise RuntimeError("network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


TRACKER_ENV = ("CONGRESS_GOV_API_KEY", "ANTHROPIC_API_KEY", "SMTP_PASSWORD", "DROPBOX_APP_KEY", "DROPBOX_REFRESH_TOKEN",
               "TRACKER_CONFIG", "TRACKER_ALLOWED_HOSTS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Tests never see the developer's real keys or config path; a test that needs one sets it."""
    for name in TRACKER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        members=MembersConfig(tracked=TRACKED),
        senate=SenateConfig(source="senate_gov"),  # most tests use the senate.gov XML fixtures
        llm=LlmConfig(provider="ollama", model="test-model", prompt_version="v1"),
        digest=DigestConfig(timezone="America/Denver"),
        storage=StorageConfig(db_path=tmp_path / "t.db", log_dir=tmp_path / "logs", output_dir=tmp_path / "digests"),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def load_crosswalk_fixtures(conn: sqlite3.Connection) -> None:
    db.store_raw(conn, "legislators", "legislators_current", "current",
                 fixture_text("legislators", "legislators-current.json"), fetched_at=FETCHED_AT)
    db.store_raw(conn, "legislators", "legislators_historical", "historical",
                 fixture_text("legislators", "legislators-historical.json"), fetched_at=FETCHED_AT)


def load_senate_fixtures(conn: sqlite3.Connection) -> None:
    for f in sorted((FIXTURES / "senate").glob("vote_119_1_*.xml")):
        roll = int(f.stem.rsplit("_", 1)[1])
        db.store_raw(
            conn, "senate_xml", "senate_vote", f"senate/119/1/{roll}", f.read_text(encoding="utf-8"),
            url=f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/{f.name}", fetched_at=FETCHED_AT,
        )


def load_house_fixture(conn: sqlite3.Connection, roll: int) -> None:
    ext = f"house/119/1/{roll}"
    db.store_raw(conn, "congress_gov", "house_vote", ext,
                 fixture_text("congress_gov", f"house_vote_119_1_{roll}.json"), fetched_at=FETCHED_AT)
    members = FIXTURES / "congress_gov" / f"house_vote_members_119_1_{roll}.json"
    if members.exists():
        db.store_raw(conn, "congress_gov", "house_vote_members", ext, members.read_text(encoding="utf-8"),
                     fetched_at=FETCHED_AT)


@pytest.fixture
def loaded(conn: sqlite3.Connection, settings: Settings) -> sqlite3.Connection:
    """A database normalized from every recorded fixture."""
    from tracker.normalize import run_normalize

    load_crosswalk_fixtures(conn)
    load_senate_fixtures(conn)
    for f in sorted((FIXTURES / "congress_gov").glob("house_vote_119_1_*.json")):
        load_house_fixture(conn, int(f.stem.rsplit("_", 1)[1]))
    for f in sorted((FIXTURES / "congress_gov").glob("bill_119_*.json")):
        _, congress, bill_type, number = f.stem.split("_")
        db.store_raw(conn, "congress_gov", "bill", f"bill/{congress}/{bill_type}/{number}",
                     f.read_text(encoding="utf-8"), fetched_at=FETCHED_AT)
    for f in sorted((FIXTURES / "congress_gov").glob("bill_summaries_119_*.json")):
        _, _, congress, bill_type, number = f.stem.split("_")
        db.store_raw(conn, "congress_gov", "bill_summaries", f"bill/{congress}/{bill_type}/{number}",
                     f.read_text(encoding="utf-8"), fetched_at=FETCHED_AT)
    run_normalize(conn, settings)
    return conn

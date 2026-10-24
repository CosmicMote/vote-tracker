"""Settings: config.toml for everything, environment variables for secrets only."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config.toml")

DEFAULT_BASE_WEIGHTS: dict[str, float] = {
    "veto_override": 1.2,
    "impeachment": 1.2,
    "speaker": 1.2,
    "final": 1.1,
    "passage": 1.0,
    "ratification": 1.0,
    "cloture": 0.9,
    "nomination": 0.8,
    "amendment": 0.6,
    "rule": 0.5,
    "motion_to_proceed": 0.5,
    "procedural": 0.5,
    "recommit": 0.4,
    "table": 0.4,
    "suspension": 0.3,
    "housekeeping": 0.0,
    "other": 0.2,
}


class MembersConfig(BaseModel):
    tracked: list[str] = Field(min_length=1)

    @field_validator("tracked")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [b.strip().upper() for b in v]


class CongressConfig(BaseModel):
    number: int = 119


class HttpConfig(BaseModel):
    user_agent: str = "vote-tracker/0.1 (personal use; you@example.com)"
    min_interval_seconds: float = 1.0
    timeout_seconds: float = 30.0
    max_attempts: int = 5


class LlmConfig(BaseModel):
    provider: Literal["ollama", "anthropic"] = "ollama"
    model: str = "llama3.1:8b"
    base_url: str = "http://localhost:11434"
    max_input_chars: int = 24000
    prompt_version: str = "v1"
    # Anthropic only. Effort level for the Messages API, and the server-side
    # refusal fallback mode ("default", or "" to disable).
    effort: str = "low"
    fallbacks: str = "default"


class DigestConfig(BaseModel):
    top_n: int = 10
    missed_vote_min_score: float = 1.0
    output_dir: Path = Path("./digests")
    timezone: str = "America/Denver"


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_user: str | None = None  # defaults to from_addr
    from_addr: str = "tracker@example.com"
    to_addr: str = "me@example.com"


class ScoringConfig(BaseModel):
    base_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_BASE_WEIGHTS))
    party_break_bonus: float = 1.5
    sponsor_bonus: float = 0.5
    tie_breaker_bonus: float = 0.5
    topic_boost: dict[str, float] = Field(default_factory=dict)
    break_min_cohesion: float = 0.80
    break_min_caucus_size: int = 5
    lopsided_share: float = 0.95
    contested_cohesion: float = 0.95

    @field_validator("base_weights")
    @classmethod
    def _merge_defaults(cls, v: dict[str, float]) -> dict[str, float]:
        return {**DEFAULT_BASE_WEIGHTS, **v}


class StorageConfig(BaseModel):
    db_path: Path = Path("./tracker.db")
    log_dir: Path = Path("./logs")
    crosswalk_url: str = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
    # Former members, used only to map votes cast before someone left office. "" disables.
    crosswalk_historical_url: str = (
        "https://unitedstates.github.io/congress-legislators/legislators-historical.json"
    )
    crosswalk_max_age_days: int = 7
    corrections_window_days: int = 14


class Secrets(BaseModel):
    congress_gov_api_key: str | None = None
    anthropic_api_key: str | None = None
    smtp_password: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            congress_gov_api_key=os.environ.get("CONGRESS_GOV_API_KEY") or None,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            smtp_password=os.environ.get("SMTP_PASSWORD") or None,
        )


class Settings(BaseModel):
    members: MembersConfig
    congress: CongressConfig = Field(default_factory=CongressConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)

    @property
    def sessions(self) -> list[str]:
        """Session identifiers ('119-1', '119-2') for the configured Congress."""
        return [f"{self.congress.number}-1", f"{self.congress.number}-2"]


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("TRACKER_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"Config file {path} not found. Copy config.example.toml to config.toml and edit it."
        )
    with path.open("rb") as f:
        data = tomllib.load(f)
    data.pop("secrets", None)  # secrets come only from the environment
    settings = Settings.model_validate(data)
    settings.secrets = Secrets.from_env()
    return settings

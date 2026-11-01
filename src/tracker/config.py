"""Settings: config.toml for everything, environment variables for secrets only."""

from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config.toml")


def _not_blank(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("can't be blank")
    return v


def _url(v: str) -> str:
    v = _not_blank(v)
    if not v.startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https://")
    return v


def _optional_url(v: str) -> str:
    return _url(v) if v.strip() else ""


NonBlank = Annotated[str, AfterValidator(_not_blank)]
Url = Annotated[str, AfterValidator(_url)]
OptionalUrl = Annotated[str, AfterValidator(_optional_url)]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")

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
    """Who the digest covers."""

    tracked: list[str] = Field(
        default_factory=list,
        description="Bioguide IDs. Chamber and LIS ID are resolved from the crosswalk. Empty until chosen.",
    )

    @field_validator("tracked")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for b in (x.strip().upper() for x in v):
            if b and b not in out:
                out.append(b)
        return out


class CongressConfig(BaseModel):
    """Which Congress to follow."""

    number: int = Field(119, ge=100, description="Congress number; 119 began on 2025-01-03.")


class HttpConfig(BaseModel):
    """How the tracker talks to Congress.gov, senate.gov and GitHub."""

    user_agent: NonBlank = Field(
        "vote-tracker/0.1 (personal use; you@example.com)",
        description="Sent with every request. Include a contact address.",
    )
    min_interval_seconds: float = Field(1.0, ge=0, description="Minimum seconds between requests to one host.")
    timeout_seconds: float = Field(30.0, gt=0, description="Per-request timeout.")
    max_attempts: int = Field(5, ge=1, description="Attempts for 429 and 5xx responses, honoring Retry-After.")


class SenateConfig(BaseModel):
    """Where Senate roll-call votes come from."""

    source: Literal["voteview", "senate_gov"] = Field(
        "voteview",
        description=(
            "voteview: Voteview's per-Congress CSV files (UCLA; reachable by scripts). "
            "senate_gov: senate.gov's official XML, which blocks many scripted clients."
        ),
    )
    voteview_url: Url = Field(
        "https://voteview.com/static/data/out", description="Voteview data root (rollcalls/, votes/, members/)."
    )


class LlmConfig(BaseModel):
    """Bill summaries. Secrets (ANTHROPIC_API_KEY) come from the environment."""

    provider: Literal["ollama", "anthropic"] = Field("ollama", description="Which summary provider to use.")
    model: NonBlank = Field("llama3.1:8b", description='Model name, e.g. "llama3.1:latest" or "claude-opus-5-5".')
    base_url: Url = Field("http://localhost:11434", description="Ollama only: server URL.")
    max_input_chars: int = Field(24000, ge=1000, description="Cap on bill text sent when no CRS summary exists.")
    prompt_version: NonBlank = Field("v1", description="Bump to regenerate summaries lazily.")
    effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        "low", description="Anthropic only: output_config.effort."
    )
    fallbacks: str = Field("default", description='Anthropic only: server-side refusal fallback; "" disables.')


class DigestConfig(BaseModel):
    """What the digest includes and where it goes."""

    top_n: int = Field(10, ge=1, description="Scored votes per member, in addition to all party breaks.")
    missed_vote_min_score: float = Field(1.0, ge=0, description="Missed votes at or above this are listed individually.")
    output_dir: Path = Field(Path("./digests"), description="Where digest HTML files are written.")
    timezone: str = Field("America/Denver", description="IANA time zone for weeks and displayed times.")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r}") from None
        return v


class EmailConfig(BaseModel):
    """Email delivery over SMTP with STARTTLS. SMTP_PASSWORD comes from the environment."""

    enabled: bool = Field(
        False, description="Email last week's digest on the [schedule] day and time while `tracker serve` runs."
    )
    smtp_host: str = Field("smtp.example.com", description="SMTP server, e.g. smtp.gmail.com.")
    smtp_port: int = Field(587, ge=1, le=65535, description="SMTP port (STARTTLS).")
    smtp_user: str | None = Field(None, description="Login name; defaults to the From address.")
    from_addr: str = Field("tracker@example.com", description="From address.")
    to_addr: str = Field("me@example.com", description="Recipient.")

    @field_validator("smtp_host", "from_addr", "to_addr")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("smtp_user")
    @classmethod
    def _user(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v else None


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _hhmm(v: str, allow_blank: bool) -> str:
    v = v.strip()
    if allow_blank and not v:
        return v
    try:
        hh, mm = v.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and len(mm) == 2):
            raise ValueError
    except ValueError:
        raise ValueError(f"use 24-hour HH:MM, e.g. 07:35 (got {v!r})") from None
    return f"{int(hh):02d}:{mm}"


class ScheduleConfig(BaseModel):
    """What `tracker serve` does on its own, in the digest time zone. Nothing runs while the server is stopped."""

    update_daily_at: str = Field(
        "02:15", description='Fetch new data and normalize every day at this time (24-hour HH:MM); "" turns it off.'
    )
    digest_weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] = Field(
        "saturday", description="Day to email last week's digest, when email is enabled."
    )
    digest_at: str = Field("07:35", description="Time to email the weekly digest (24-hour HH:MM).")

    @field_validator("update_daily_at")
    @classmethod
    def _update_time(cls, v: str) -> str:
        return _hhmm(v, allow_blank=True)

    @field_validator("digest_at")
    @classmethod
    def _digest_time(cls, v: str) -> str:
        return _hhmm(v, allow_blank=False)


class BackupConfig(BaseModel):
    """Daily backup of settings and data to Dropbox while `tracker serve` runs, in the digest time zone.
    DROPBOX_APP_KEY and DROPBOX_REFRESH_TOKEN come from the environment (see `tracker dropbox-auth`)."""

    enabled: bool = Field(False, description="Upload a backup to Dropbox every day at daily_at.")
    daily_at: str = Field("03:30", description="Time of the daily backup (24-hour HH:MM).")
    dropbox_folder: str = Field(
        "/vote-tracker",
        description="Dropbox folder for backups. For an App folder app, it is inside Apps/<app name>.",
    )
    keep: int = Field(14, ge=1, description="Backups to keep in the folder; older ones are deleted after each upload.")

    @field_validator("daily_at")
    @classmethod
    def _time(cls, v: str) -> str:
        return _hhmm(v, allow_blank=False)

    @field_validator("dropbox_folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        v = v.strip().strip("/")
        if not v:
            raise ValueError("enter a folder, e.g. /vote-tracker")
        return f"/{v}"


class ScoringConfig(BaseModel):
    """Weights and thresholds for picking notable votes."""

    base_weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_BASE_WEIGHTS), description="Base weight per vote class."
    )
    party_break_bonus: float = Field(1.5, description="Added when the member broke from their caucus.")
    sponsor_bonus: float = Field(0.5, description="Added when the member sponsored or cosponsored the bill.")
    tie_breaker_bonus: float = Field(0.5, description="Added when the Vice President broke a tie.")
    topic_boost: dict[str, float] = Field(
        default_factory=dict, description="Congress.gov policy area -> added score."
    )
    break_min_cohesion: float = Field(0.80, ge=0, le=1, description="A break only counts against a caucus at least this united.")
    break_min_caucus_size: int = Field(5, ge=1, description="Skip break detection for smaller caucuses.")
    lopsided_share: float = Field(0.95, ge=0.5, le=1, description="Exclude votes with a yea share above this (or below 1 - this).")
    contested_cohesion: float = Field(0.95, ge=0, le=1, description='Caucus cohesion below this makes a vote "contested".')

    @field_validator("base_weights")
    @classmethod
    def _merge_defaults(cls, v: dict[str, float]) -> dict[str, float]:
        return {**DEFAULT_BASE_WEIGHTS, **v}


class StorageConfig(BaseModel):
    """Files and data sources."""

    db_path: Path = Field(Path("./tracker.db"), description="SQLite database file.")
    log_dir: Path = Field(Path("./logs"), description="Directory for tracker.log.")
    crosswalk_url: Url = Field(
        "https://unitedstates.github.io/congress-legislators/legislators-current.json",
        description="congress-legislators current members (JSON).",
    )
    crosswalk_historical_url: OptionalUrl = Field(
        "https://unitedstates.github.io/congress-legislators/legislators-historical.json",
        description='Former members, used only to map votes cast before someone left office. "" disables.',
    )
    crosswalk_max_age_days: int = Field(7, ge=1, description="Refresh the crosswalk when older than this.")
    corrections_window_days: int = Field(14, ge=0, description="Re-fetch votes this recent to pick up corrections.")


class Secrets(BaseModel):
    congress_gov_api_key: str | None = None
    anthropic_api_key: str | None = None
    smtp_password: str | None = None
    dropbox_app_key: str | None = None
    dropbox_refresh_token: str | None = None

    @property
    def dropbox_ready(self) -> bool:
        return bool(self.dropbox_app_key and self.dropbox_refresh_token)

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            congress_gov_api_key=os.environ.get("CONGRESS_GOV_API_KEY") or None,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            smtp_password=os.environ.get("SMTP_PASSWORD") or None,
            dropbox_app_key=os.environ.get("DROPBOX_APP_KEY") or None,
            dropbox_refresh_token=os.environ.get("DROPBOX_REFRESH_TOKEN") or None,
        )


class Settings(BaseModel):
    members: MembersConfig = Field(default_factory=MembersConfig)
    congress: CongressConfig = Field(default_factory=CongressConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    senate: SenateConfig = Field(default_factory=SenateConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)

    @property
    def sessions(self) -> list[str]:
        """Session identifiers ('119-1', '119-2') for the configured Congress."""
        return [f"{self.congress.number}-1", f"{self.congress.number}-2"]


def email_problems(email: EmailConfig) -> list[dict]:
    """Why the email settings can't send, as [{"loc": [...], "msg": ...}]. Empty means ready.

    These rules block saving in the web app and stop a send with a clear reason, but never stop
    a config from loading: a half-finished [email] section must not lock you out of the app.
    """
    out: list[dict] = []

    def problem(field: str, msg: str) -> None:
        out.append({"loc": ["email", field], "msg": msg})

    host = email.smtp_host
    if host and not _HOST_RE.match(host):
        problem("smtp_host", "must be a host name such as smtp.gmail.com (no scheme, port or spaces)")
    for field in ("from_addr", "to_addr"):
        value = getattr(email, field)
        if value and not _EMAIL_RE.match(value):
            problem(field, "must be an email address like name@domain.com")
    if email.enabled:
        if not host:
            problem("smtp_host", "required when email is enabled")
        elif host.endswith("example.com"):
            problem("smtp_host", f"{host} is a placeholder; enter your SMTP server")
        for field in ("from_addr", "to_addr"):
            value = getattr(email, field)
            if not value:
                problem(field, "required when email is enabled")
            elif value.endswith("@example.com"):
                problem(field, f"{value} is a placeholder; enter a real address")
    return out


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
    for p in email_problems(settings.email):
        logging.getLogger(__name__).warning("config %s: %s.%s %s", path, *p["loc"], p["msg"])
    return settings

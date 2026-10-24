from pathlib import Path

import pytest
from pydantic import ValidationError

from tracker.config import Settings, load_settings
from tracker.config_store import save_settings

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(EXAMPLE.read_text())
    return p


def test_unchanged_save_leaves_file_byte_identical(cfg):
    before = cfg.read_text()
    save_settings(cfg, load_settings(cfg))
    assert cfg.read_text() == before
    assert not cfg.with_suffix(".toml.bak").exists()


def test_changes_touch_only_their_lines_and_keep_comments(cfg):
    before = cfg.read_text().splitlines()
    s = load_settings(cfg)
    s.digest.top_n = 7
    s.email.smtp_user = "me@example.com"
    s.scoring.base_weights["cloture"] = 1.0
    s.scoring.topic_boost = {"Taxation": 0.3}
    save_settings(cfg, s)
    after = cfg.read_text().splitlines()
    changed = [line for line in after if line not in before]
    assert any(line.startswith("top_n = 7") and "# scored votes per member" in line for line in changed)
    assert 'smtp_user = "me@example.com"' in changed
    assert "cloture = 1.0" in changed and "Taxation = 0.3" in changed
    assert len(changed) == 4
    assert cfg.with_suffix(".toml.bak").read_text().splitlines() == before
    again = load_settings(cfg)
    assert (again.digest.top_n, again.email.smtp_user, again.scoring.base_weights["cloture"]) == (7, "me@example.com", 1.0)


def test_resetting_to_defaults_and_null_removes_lines(cfg):
    s = load_settings(cfg)
    s.scoring.base_weights["cloture"] = 1.0
    s.email.smtp_user = "x@example.com"
    save_settings(cfg, s)
    s.scoring.base_weights["cloture"] = 0.9  # back to the default
    s.email.smtp_user = None
    save_settings(cfg, s)
    text = cfg.read_text()
    assert "\ncloture = " not in text and "\nsmtp_user = " not in text


def test_secrets_never_written(cfg, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")
    s = load_settings(cfg)
    s.digest.top_n = 3
    save_settings(cfg, s)
    assert "hunter2" not in cfg.read_text() and "secrets" not in cfg.read_text()


def test_invalid_values_rejected_before_saving():
    with pytest.raises(ValidationError):
        Settings.model_validate({"members": {"tracked": ["X"]}, "digest": {"timezone": "Mars/Olympus"}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"members": {"tracked": [" "]}})


# --- validation rules -------------------------------------------------------------------

from tracker.config import EmailConfig, email_problems  # noqa: E402


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, []),  # disabled with placeholders: fine
        ({"enabled": True}, [("smtp_host", "placeholder"), ("from_addr", "placeholder"), ("to_addr", "placeholder")]),
        ({"enabled": True, "smtp_host": "", "from_addr": "a@b.co", "to_addr": "c@d.co"}, [("smtp_host", "required")]),
        ({"enabled": True, "smtp_host": "smtp.gmail.com", "from_addr": "a@b.co", "to_addr": "c@d.co"}, []),
        ({"smtp_host": "https://smtp.gmail.com:587"}, [("smtp_host", "host name")]),
        ({"to_addr": "not an address"}, [("to_addr", "email address")]),
    ],
)
def test_email_problems(fields, expected):
    found = [(p["loc"][1], p["msg"]) for p in email_problems(EmailConfig(**fields))]
    assert len(found) == len(expected)
    for (field, msg), (want_field, want_text) in zip(found, expected, strict=True):
        assert field == want_field and want_text in msg


def test_config_with_email_problems_still_loads(cfg, caplog):
    text = cfg.read_text().replace("enabled = false", "enabled = true")
    cfg.write_text(text.replace('smtp_host = "smtp.example.com"', 'smtp_host = ""'))
    s = load_settings(cfg)
    assert s.email.enabled is True
    assert "required when email is enabled" in caplog.text


@pytest.mark.parametrize(
    "section,field,value,message",
    [
        ("http", "user_agent", "  ", "can't be blank"),
        ("llm", "base_url", "localhost:11434", "http://"),
        ("storage", "crosswalk_historical_url", "ftp://x", "http://"),
        ("schedule", "digest_at", "7pm", "HH:MM"),
    ],
)
def test_startup_rules(section, field, value, message):
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate({"members": {"tracked": ["X"]}, section: {field: value}})
    if field == "crosswalk_historical_url":
        assert Settings.model_validate({"members": {"tracked": ["X"]}, section: {field: ""}})  # "" disables

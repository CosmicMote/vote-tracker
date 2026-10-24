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

"""CLI stages end to end against fixtures, and the run_log rows each command writes."""

import sqlite3

from typer.testing import CliRunner

from conftest import load_crosswalk_fixtures, load_house_fixture, load_senate_fixtures
from tracker import db
from tracker.cli import app


def write_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[members]
tracked = ["D000197", "B001267", "H000273"]
[senate]
source = "senate_gov"
[digest]
output_dir = "{tmp_path / 'digests'}"
[storage]
db_path = "{tmp_path / 'tracker.db'}"
log_dir = "{tmp_path / 'logs'}"
"""
    )
    return cfg


def test_offline_stages_and_run_log(tmp_path):
    cfg = write_config(tmp_path)
    conn = db.connect(tmp_path / "tracker.db")
    db.init_db(conn)
    load_crosswalk_fixtures(conn)
    load_senate_fixtures(conn)
    load_house_fixture(conn, 242)
    conn.commit()
    conn.close()

    runner = CliRunner()
    for args in (["init-db"], ["normalize"], ["analyze", "--week", "2025-11-12"],
                 ["digest", "--week", "2025-11-12"], ["explain-vote", "senate", "1", "617"]):
        result = runner.invoke(app, ["-c", str(cfg), *args])
        assert result.exit_code == 0, (args, result.output)
    assert "threshold:  0.6" in result.output
    assert (tmp_path / "digests" / "2025-11-10.html").exists()

    conn = sqlite3.connect(tmp_path / "tracker.db")
    rows = conn.execute("SELECT command, status FROM run_log ORDER BY id").fetchall()
    assert rows == [("init-db", "ok"), ("normalize", "ok"), ("analyze", "ok"), ("digest", "ok"), ("explain-vote", "ok")]


def test_missing_vote_exits_nonzero(tmp_path):
    cfg = write_config(tmp_path)
    result = CliRunner().invoke(app, ["-c", str(cfg), "explain-vote", "house", "1", "99999"])
    assert result.exit_code == 1


def test_missing_config_is_a_clear_error(tmp_path):
    result = CliRunner().invoke(app, ["-c", str(tmp_path / "nope.toml"), "init-db"])
    assert result.exit_code == 2
    assert "config.example.toml" in result.output

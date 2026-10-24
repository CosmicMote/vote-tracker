"""Web API tests: FastAPI TestClient against the fixture database, with a fake summary provider."""

import json
import sqlite3
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import load_crosswalk_fixtures, load_house_fixture, load_senate_fixtures
from tracker import analyze, db
from tracker.normalize.common import get_or_create_bill
from tracker.web.app import create_app

HEADERS = {"X-Tracker-Client": "test"}
CONFIG = """\
# Test config; this comment must survive saves.
[members]
tracked = ["D000197", "B001267", "H000273"]  # DeGette, Bennet, Hickenlooper

[senate]
source = "senate_gov"

[digest]
top_n = 10                     # scored votes per member
output_dir = "{tmp}/digests"
timezone = "America/Denver"

[storage]
db_path = "{tmp}/tracker.db"
log_dir = "{tmp}/logs"
"""


class FakeProvider:
    name = "fake:web"

    def complete_json(self, system, user, schema):
        return json.dumps({"plain_title": "Funds the government through January",
                           "what_it_does": ["Extends funding at current levels.", "Sets a new deadline."],
                           "caveat": None})


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setattr("tracker.llm.make_provider", lambda settings: FakeProvider())
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG.format(tmp=tmp_path))
    conn = db.connect(tmp_path / "tracker.db")
    db.init_db(conn)
    load_crosswalk_fixtures(conn)
    load_senate_fixtures(conn)
    load_house_fixture(conn, 242)
    conn.commit()
    from tracker.config import load_settings
    from tracker.normalize import run_normalize

    run_normalize(conn, load_settings(cfg))
    conn.close()
    app = create_app(cfg, allowed_hosts=["testserver"], start_scheduler=False)
    with TestClient(app) as client:
        yield client, app.state.tracker, cfg, tmp_path


def wait_idle(state, timeout=10):
    deadline = time.time() + timeout
    while not state.runner.idle():
        assert time.time() < deadline, "jobs did not finish"
        time.sleep(0.02)


def test_weeks_start_at_the_119th_congress(app_env):
    client, state, *_ = app_env
    r = client.get("/api/weeks").json()
    assert r["first"] == "2024-12-30"  # the week containing 2025-01-03
    assert r["weeks"][0]["start"] == r["default"] == r["last"]
    assert r["weeks"][-1]["start"] == "2024-12-30"
    last = analyze.last_full_week("America/Denver").start
    assert r["default"] == last.isoformat()


@pytest.mark.parametrize("week,code", [("2024-12-23", 400), ("not-a-date", 400), ("2999-01-01", 400)])
def test_week_bounds_enforced(app_env, week, code):
    client, *_ = app_env
    assert client.get(f"/api/digest/{week}").status_code == code


def test_digest_analyzes_once_until_something_changes(app_env):
    client, *_ = app_env
    first = client.get("/api/digest/2025-11-12").json()
    assert first["week"] == {"start": "2025-11-10", "end": "2025-11-16", "label": "Nov 10, 2025 – Nov 16, 2025"}
    assert first["reanalyzed"] is True
    names = [m["name"] for m in first["digest"]["members"]]
    assert names == ["Diana DeGette", "Michael F. Bennet", "John W. Hickenlooper"]
    assert client.get("/api/digest/2025-11-10").json()["reanalyzed"] is False


def test_summaries_fill_in_from_background_job(app_env):
    client, state, _, tmp = app_env
    conn = db.connect(tmp / "tracker.db")
    bill = get_or_create_bill(conn, "119", "hr", "5371")
    conn.execute("INSERT INTO bill_summary VALUES (?, 'crs', '00', '2025-09-19', 'Continuing appropriations.')", (bill,))
    conn.commit()
    conn.close()
    r = client.get("/api/digest/2025-11-10").json()
    assert r["summaries"]["pending"] == 1 and r["summaries"]["job"]["kind"] == "summarize"
    wait_idle(state)
    r = client.get("/api/digest/2025-11-10").json()
    assert r["summaries"]["pending"] == 0 and r["summaries"]["job"]["status"] == "ok"
    bills = r["digest"]["members"][1]["notable"][0]["bills"]
    assert bills[0]["bullets"] == ["Extends funding at current levels.", "Sets a new deadline."]


def test_email_version_and_send_guard(app_env):
    client, *_ = app_env
    html = client.get("/api/digest/2025-11-10/email")
    assert html.status_code == 200 and html.text.startswith("<!DOCTYPE html>")
    assert client.post("/api/digest/2025-11-10/send", headers=HEADERS).status_code == 409  # email disabled


def test_mutations_require_client_header_and_local_host(app_env):
    client, *_ = app_env
    assert client.post("/api/jobs", json={"kind": "normalize"}).status_code == 403
    assert client.get("/api/weeks", headers={"Host": "evil.example"}).status_code == 400


def test_members_saved_to_config_and_flags_updated(app_env):
    client, state, cfg, tmp = app_env
    assert [m["bioguide"] for m in client.get("/api/members").json()["members"]] == ["D000197", "B001267", "H000273"]
    bad = client.put("/api/members", json={"tracked": ["X000000"]}, headers=HEADERS)
    assert bad.status_code == 422
    r = client.put("/api/members", json={"tracked": ["d000197", "B001267"]}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["tracked"] == ["D000197", "B001267"]
    assert r.json()["job"] is None  # no API key, so no sponsorship refresh
    text = cfg.read_text()
    assert 'tracked = ["D000197", "B001267"]' in text and "this comment must survive" in text
    assert "# scored votes per member" in text
    conn = sqlite3.connect(tmp / "tracker.db")
    assert [r[0] for r in conn.execute("SELECT bioguide_id FROM legislator WHERE tracked=1 ORDER BY 1")] == ["B001267", "D000197"]
    # A members change re-analyzes the next digest view.
    client.get("/api/digest/2025-11-10")
    assert len(client.get("/api/digest/2025-11-10").json()["digest"]["members"]) == 2


def test_legislators_list(app_env):
    client, *_ = app_env
    people = client.get("/api/legislators").json()["legislators"]
    co = [p for p in people if p["state"] == "CO"]
    assert [p["chamber"] for p in co][:2] == ["senate", "senate"]
    assert {"bioguide": "D000197", "chamber": "house", "district": "1"}.items() <= next(
        p for p in co if p["bioguide"] == "D000197").items()


def test_config_round_trip_and_validation(app_env):
    client, state, cfg, _ = app_env
    c = client.get("/api/config").json()
    assert c["values"]["digest"]["top_n"] == 10
    assert "properties" in c["schema"] and "DigestConfig" in c["schema"]["$defs"]
    assert c["secrets"] == {"CONGRESS_GOV_API_KEY": False, "ANTHROPIC_API_KEY": False, "SMTP_PASSWORD": False}
    values = c["values"]
    values["digest"]["timezone"] = "Mars/Olympus"
    bad = client.put("/api/config", json={"values": values}, headers=HEADERS)
    assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["digest", "timezone"]
    values["digest"]["timezone"] = "America/Denver"
    values["digest"]["top_n"] = 4
    values["scoring"]["topic_boost"] = {"Economics and Public Finance": 0.25}
    ok = client.put("/api/config", json={"values": values}, headers=HEADERS)
    assert ok.status_code == 200 and ok.json()["values"]["digest"]["top_n"] == 4
    text = cfg.read_text()
    assert "top_n = 4" in text and "# scored votes per member" in text and "Economics and Public Finance" in text
    assert state.settings.digest.top_n == 4
    assert (cfg.parent / "config.toml.bak").exists()


def test_jobs_endpoint_runs_normalize(app_env):
    client, state, *_ = app_env
    job = client.post("/api/jobs", json={"kind": "normalize"}, headers=HEADERS).json()
    assert job["kind"] == "normalize"
    wait_idle(state)
    r = client.get("/api/jobs").json()
    assert r["jobs"][0]["status"] == "ok"
    assert r["runs"][0]["command"] == "normalize"
    assert client.post("/api/jobs", json={"kind": "update"}, headers=HEADERS).status_code == 409  # no API key


def test_status(app_env):
    client, *_ = app_env
    s = client.get("/api/status").json()
    assert s["votes"]["senate"]["votes"] == 5 and s["has_crosswalk"] is True
    assert s["weeks"]["first"] == "2024-12-30"
    assert date.fromisoformat(s["weeks"]["last"]).weekday() == 0


def _crs(tmp, bill_type, number):
    conn = db.connect(tmp / "tracker.db")
    bill = get_or_create_bill(conn, "119", bill_type, number)
    conn.execute("INSERT INTO bill_summary VALUES (?, 'crs', '00', '2025-06-01', 'Official summary.')", (bill,))
    conn.commit()
    conn.close()


def test_viewing_another_week_drops_its_queued_summaries(app_env):
    import threading

    client, state, _, tmp = app_env
    _crs(tmp, "hr", "5371")  # in the week of Nov 10
    _crs(tmp, "hr", "1")  # in the week of Jun 30 (Senate roll 372)
    release = threading.Event()
    state.runner.submit("blocker", "Long-running job", lambda conn, s: release.wait(10) and None)

    nov = client.get("/api/digest/2025-11-10").json()["summaries"]
    assert nov["job"]["status"] == "queued" and nov["waiting_for"] == "Long-running job"

    jun = client.get("/api/digest/2025-06-30").json()["summaries"]
    assert jun["job"]["status"] == "queued"
    nov_job = state.runner.find("summarize:2025-11-10")
    assert nov_job.status == "cancelled"  # dropped: the user moved on before it started

    release.set()
    wait_idle(state)
    conn = sqlite3.connect(tmp / "tracker.db")
    summarized = {r[0] for r in conn.execute("SELECT b.number FROM llm_summary s JOIN bill b ON b.id = s.bill_id")}
    assert summarized == {"1"}  # only the week being viewed was summarized

    # Going back re-queues the dropped week, and it runs.
    back = client.get("/api/digest/2025-11-10").json()["summaries"]
    assert back["job"]["status"] in ("queued", "running", "ok") and back["job"]["id"] != nov_job.id
    wait_idle(state)
    assert client.get("/api/digest/2025-11-10").json()["summaries"]["pending"] == 0


def test_running_summary_is_never_cancelled(app_env, monkeypatch):
    import threading

    client, state, _, tmp = app_env
    _crs(tmp, "hr", "5371")
    started, release = threading.Event(), threading.Event()

    class SlowProvider(FakeProvider):
        def complete_json(self, system, user, schema):
            started.set()
            release.wait(10)
            return super().complete_json(system, user, schema)

    monkeypatch.setattr("tracker.llm.make_provider", lambda settings: SlowProvider())
    client.get("/api/digest/2025-11-10")
    assert started.wait(5)
    client.get("/api/digest/2025-06-30")  # another week while Nov is mid-run
    assert state.runner.find("summarize:2025-11-10").status == "running"
    release.set()
    wait_idle(state)
    assert state.runner.find("summarize:2025-11-10").status == "ok"


def test_job_runner_skips_cancelled_jobs(app_env):
    import threading

    _, state, *_ = app_env
    release, ran = threading.Event(), []
    state.runner.submit("blocker", "block", lambda conn, s: release.wait(10) and None)
    state.runner.submit("x", "first", lambda conn, s: ran.append("first"))
    state.runner.submit("y", "second", lambda conn, s: ran.append("second"))
    assert [j.label for j in state.runner.cancel_queued(lambda j: j.kind == "x")] == ["first"]
    release.set()
    wait_idle(state)
    assert ran == ["second"]


def test_live_validation_reports_field_errors_without_saving(app_env):
    client, state, cfg, _ = app_env
    before = cfg.read_text()
    values = client.get("/api/config").json()["values"]
    values["digest"]["top_n"] = "ten"  # typed letters into a number field
    values["schedule"]["digest_at"] = "7pm"
    values["email"]["enabled"] = True  # with the placeholder host and addresses
    r = client.post("/api/config/validate", json={"values": values}, headers=HEADERS)
    assert r.status_code == 200
    errors = {tuple(e["loc"]): e["msg"] for e in r.json()["errors"]}
    assert ("digest", "top_n") in errors and "integer" in errors[("digest", "top_n")]
    assert ("email", "smtp_host") in errors  # readiness problems are reported alongside, not later
    assert errors[("schedule", "digest_at")].startswith("use 24-hour HH:MM")  # no "Value error," prefix
    assert cfg.read_text() == before  # nothing saved
    values["digest"]["top_n"], values["schedule"]["digest_at"] = 10, "07:35"
    r = client.post("/api/config/validate", json={"values": values}, headers=HEADERS).json()["errors"]
    assert {tuple(e["loc"]) for e in r} == {("email", "smtp_host"), ("email", "from_addr"), ("email", "to_addr")}
    assert client.put("/api/config", json={"values": values}, headers=HEADERS).status_code == 422
    values["email"]["enabled"] = False  # unchecking Enabled makes it saveable again
    assert client.post("/api/config/validate", json={"values": values}, headers=HEADERS).json()["errors"] == []


def test_send_refused_while_email_settings_incomplete(app_env):
    client, state, *_ = app_env
    state.settings.email.enabled = True
    state.settings.secrets.smtp_password = "pw"
    r = client.post("/api/digest/2025-11-10/send", headers=HEADERS)
    assert r.status_code == 409 and "smtp_host" in r.json()["detail"]

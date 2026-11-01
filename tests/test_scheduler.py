"""The web app's scheduler, driven by a fake clock. Times are America/Denver (the test config)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from test_web import app_env  # noqa: F401 - shared fixture: app + fixture database
from test_web import wait_idle
from tracker import analyze, pipeline
from tracker.web.scheduler import Scheduler, digest_period, latest_slot

DENVER = ZoneInfo("America/Denver")


def at(*args) -> datetime:
    return datetime(*args, tzinfo=DENVER)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.parametrize(
    "now,hhmm,weekday,expected",
    [
        (at(2026, 10, 28, 9, 0), "02:15", None, at(2026, 10, 28, 2, 15)),  # earlier today
        (at(2026, 10, 28, 1, 0), "02:15", None, at(2026, 10, 27, 2, 15)),  # not yet today
        (at(2026, 10, 31, 7, 35), "07:35", 5, at(2026, 10, 31, 7, 35)),  # exactly on the slot
        (at(2026, 10, 31, 7, 34), "07:35", 5, at(2026, 10, 24, 7, 35)),  # a minute early
        (at(2026, 10, 28, 12, 0), "07:35", 5, at(2026, 10, 24, 7, 35)),  # Wednesday: last Saturday
    ],
)
def test_latest_slot(now, hhmm, weekday, expected):
    assert latest_slot(now, hhmm, weekday) == expected


def test_digest_covers_the_last_full_week_before_the_slot():
    p = digest_period(at(2026, 10, 31, 7, 35), "America/Denver")  # Saturday
    assert (p.start, p.end) == (date(2026, 10, 19), date(2026, 10, 25))
    assert p == analyze.last_full_week("America/Denver", at(2026, 10, 31, 7, 35))


@pytest.fixture
def sched(app_env, monkeypatch):  # noqa: F811
    client, state, cfg, tmp = app_env
    sent = []
    monkeypatch.setattr("tracker.deliver.send_digest", lambda s, subject, path: sent.append((subject, path)))
    clock = Clock(at(2026, 10, 28, 12, 0))  # a Wednesday
    return Scheduler(state, clock=clock), clock, state, sent


def enable_email(state):
    e = state.settings.email
    e.enabled, e.smtp_host, e.from_addr, e.to_addr = True, "smtp.mail.test", "tracker@mail.test", "jeff@mail.test"
    state.settings.secrets.smtp_password = "pw"


def test_enabling_email_waits_for_the_next_slot(sched, monkeypatch):
    scheduler, clock, state, sent = sched
    assert scheduler.tick() == []  # email off: nothing
    enable_email(state)
    assert scheduler.tick() == []  # last Saturday's slot predates enabling: no surprise send
    clock.now = at(2026, 10, 31, 7, 34)
    assert scheduler.tick() == []
    clock.now = at(2026, 10, 31, 7, 35)
    assert scheduler.tick() == ["weekly"]
    wait_idle(state)
    assert len(sent) == 1 and "week of Oct 19, 2026" in sent[0][0]
    clock.now = at(2026, 10, 31, 9, 0)
    assert scheduler.tick() == []  # once per slot
    clock.now = at(2026, 11, 7, 7, 40)
    assert scheduler.tick() == ["weekly"]  # next Saturday: the following week
    wait_idle(state)
    assert "week of Oct 26, 2026" in sent[1][0]


def test_missed_slot_is_caught_up_once(sched, monkeypatch):
    scheduler, clock, state, sent = sched
    enable_email(state)
    scheduler.tick()  # Wednesday Oct 28: email enabled
    clock.now = at(2026, 11, 2, 8, 0)  # asleep all weekend; back on Monday
    assert scheduler.tick() == ["weekly"]
    wait_idle(state)
    assert "week of Oct 19, 2026" in sent[0][0]
    assert scheduler.tick() == []


def test_week_already_sent_is_skipped(sched, monkeypatch):
    scheduler, clock, state, sent = sched
    enable_email(state)
    scheduler.tick()
    with state.conn() as conn:
        pipeline.mark_sent(conn, analyze.week_containing(date(2026, 10, 19), "America/Denver"), "me@example.com")
    clock.now = at(2026, 10, 31, 7, 35)
    assert scheduler.tick() == []
    assert sent == []


def test_disabling_email_resets_the_wait(sched, monkeypatch):
    scheduler, clock, state, sent = sched
    enable_email(state)
    scheduler.tick()  # enabled Wednesday Oct 28
    state.settings.email.enabled = False
    clock.now = at(2026, 11, 3, 12, 0)
    scheduler.tick()  # disabled over the Oct 31 slot
    state.settings.email.enabled = True
    assert scheduler.tick() == []  # re-enabled on Tuesday: Oct 31 is not sent retroactively


def test_daily_update_needs_the_api_key_and_runs_once_per_day(sched):
    scheduler, clock, state, _ = sched
    assert scheduler.tick() == []  # no CONGRESS_GOV_API_KEY
    state.settings.secrets.congress_gov_api_key = "key"
    state.runner.submit("blocker", "hold", lambda c, s: None)  # the update job itself is not under test
    assert "update" in scheduler.tick()
    assert "update" not in scheduler.tick()
    clock.now = at(2026, 10, 29, 2, 15)
    assert "update" in scheduler.tick()
    state.settings.schedule.update_daily_at = ""
    clock.now = at(2026, 10, 30, 3, 0)
    assert "update" not in scheduler.tick()
    state.runner.cancel_queued(lambda j: j.kind == "update")


def test_status_describes_the_schedule(app_env):  # noqa: F811
    client, state, *_ = app_env
    sch = client.get("/api/status").json()["schedule"]
    assert sch["running"] is False  # tests start the app without the scheduler thread
    assert sch["digest"]["email_enabled"] is False and sch["digest"]["next"].startswith("Sat ")
    assert sch["update"]["needs_key"] is True


def test_run_weekly_cli_does_not_resend(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from test_cli import write_config
    from tracker.cli import app as cli_app

    cfg = write_config(tmp_path)
    cfg.write_text(cfg.read_text() + '\n[email]\nenabled = true\nsmtp_host = "smtp.mail.test"\n'
                   'from_addr = "tracker@mail.test"\nto_addr = "jeff@mail.test"\n')
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    sent = []
    monkeypatch.setattr("tracker.deliver.send_digest", lambda s, subject, path: sent.append(subject))
    monkeypatch.setattr("tracker.pipeline.ingest", lambda *a, **k: {"status": "ok", "stats": {}})
    monkeypatch.setattr("tracker.pipeline.summarize_week", lambda *a, **k: {"status": "ok", "stats": {}})
    from conftest import load_crosswalk_fixtures
    from tracker import db

    conn = db.connect(tmp_path / "tracker.db")
    db.init_db(conn)
    load_crosswalk_fixtures(conn)
    conn.commit()
    conn.close()
    runner = CliRunner()
    first = runner.invoke(cli_app, ["-c", str(cfg), "run-weekly"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli_app, ["-c", str(cfg), "run-weekly"])
    assert "not sent: already sent" in second.output
    forced = runner.invoke(cli_app, ["-c", str(cfg), "run-weekly", "--force"])
    assert forced.exit_code == 0
    assert len(sent) == 2


def test_incomplete_email_settings_hold_the_send_until_fixed(sched):
    scheduler, clock, state, sent = sched
    enable_email(state)
    scheduler.tick()  # Wednesday: enabled
    state.settings.email.smtp_host = "smtp.example.com"  # placeholder
    clock.now = at(2026, 10, 31, 7, 35)
    assert scheduler.tick() == []  # not ready: nothing sent, slot not used up
    state.settings.email.smtp_host = "smtp.mail.test"
    clock.now = at(2026, 10, 31, 9, 0)
    assert scheduler.tick() == ["weekly"]  # fixed the same morning: the slot goes out


def test_no_members_hold_the_send_until_chosen(sched):
    scheduler, clock, state, sent = sched
    enable_email(state)
    scheduler.tick()  # Wednesday: enabled
    tracked, state.settings.members.tracked = state.settings.members.tracked, []
    clock.now = at(2026, 10, 31, 7, 35)
    assert scheduler.tick() == []  # nobody to report on: nothing sent, slot not used up
    state.settings.members.tracked = tracked
    clock.now = at(2026, 10, 31, 9, 0)
    assert scheduler.tick() == ["weekly"]

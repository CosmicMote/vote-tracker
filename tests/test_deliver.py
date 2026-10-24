import smtplib

import pytest

from tracker.deliver import DeliveryError, send_digest


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, fail=False):
        self.host, self.port = host, port
        self.events: list[str] = []
        self.sent = []
        self.fail = fail
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.events.append("starttls")

    def login(self, user, password):
        self.events.append(f"login:{user}")

    def send_message(self, msg):
        if self.fail:
            raise smtplib.SMTPServerDisconnected("gone")
        self.sent.append(msg)


@pytest.fixture
def digest_file(tmp_path):
    p = tmp_path / "2025-11-10.html"
    p.write_text("<html><body>digest</body></html>")
    return p


def test_sends_html_over_starttls(settings, digest_file):
    settings.secrets.smtp_password = "pw"
    FakeSMTP.instances.clear()
    send_digest(settings, "Subject line", digest_file, smtp_factory=FakeSMTP)
    smtp = FakeSMTP.instances[0]
    assert smtp.events == ["starttls", f"login:{settings.email.from_addr}"]
    msg = smtp.sent[0]
    assert msg["Subject"] == "Subject line" and msg["To"] == settings.email.to_addr
    assert "digest" in msg.get_body(("html",)).get_content()


def test_delivery_failure_keeps_file(settings, digest_file):
    settings.secrets.smtp_password = "pw"
    with pytest.raises(DeliveryError):
        send_digest(settings, "s", digest_file, smtp_factory=lambda *a, **k: FakeSMTP(*a, fail=True, **k))
    assert digest_file.exists()


def test_missing_password(settings, digest_file):
    settings.secrets.smtp_password = None
    with pytest.raises(DeliveryError):
        send_digest(settings, "s", digest_file, smtp_factory=FakeSMTP)

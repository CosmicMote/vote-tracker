"""Stage 6: email the rendered digest over SMTP with STARTTLS. Never touches the file on disk."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .config import Settings


class DeliveryError(Exception):
    pass


def build_message(settings: Settings, subject: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email.from_addr
    msg["To"] = settings.email.to_addr
    msg.set_content("This digest is HTML. Open it in an HTML-capable mail client, or open the saved file.")
    msg.add_alternative(html, subtype="html")
    return msg


def send_digest(settings: Settings, subject: str, path: Path, smtp_factory=smtplib.SMTP) -> None:
    cfg = settings.email
    if not settings.secrets.smtp_password:
        raise DeliveryError("SMTP_PASSWORD is not set")
    msg = build_message(settings, subject, path.read_text(encoding="utf-8"))
    try:
        with smtp_factory(cfg.smtp_host, cfg.smtp_port, timeout=60) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg.smtp_user or cfg.from_addr, settings.secrets.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise DeliveryError(f"sending to {cfg.to_addr} via {cfg.smtp_host}:{cfg.smtp_port} failed: {e}") from e

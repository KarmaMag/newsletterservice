"""Email delivery via SMTP (stdlib) used to send campaigns and test previews."""
import logging
import os
import ssl
import smtplib
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


class MailerConfigError(RuntimeError):
    pass


def smtp_config() -> dict:
    return {
        "host": os.environ.get("SMTP_HOST"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER"),
        "password": os.environ.get("SMTP_PASSWORD"),
        "from_email": os.environ.get("SMTP_FROM_EMAIL"),
        "from_name": os.environ.get("SMTP_FROM_NAME", "Newsletter"),
    }


def is_configured() -> bool:
    cfg = smtp_config()
    return bool(cfg["host"] and cfg["from_email"])


def build_message(from_email: str, from_name: str, to_email: str,
                  subject: str, html: str, preheader: Optional[str] = None,
                  unsubscribe_url: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to_email
    if preheader:
        html = f'<div style="display:none">{preheader}</div>{html}'
    footer = ""
    if unsubscribe_url:
        footer = (
            f'<hr><p style="font-size:12px;color:#888">'
            f'<a href="{unsubscribe_url}">Unsubscribe</a></p>'
        )
    plain = f"{subject}\n\n{unsubscribe_url or ''}"
    msg.set_content(plain)
    msg.add_alternative(f"<html><body>{html}{footer}</body></html>", subtype="html")
    return msg


def send_email(to_email: str, subject: str, html: str, preheader: Optional[str] = None,
               unsubscribe_url: Optional[str] = None) -> None:
    cfg = smtp_config()
    if not is_configured():
        raise MailerConfigError(
            "SMTP is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, "
            "SMTP_PASSWORD and SMTP_FROM_EMAIL in the environment to enable sending."
        )
    msg = build_message(
        cfg["from_email"], cfg["from_name"], to_email, subject, html, preheader,
        unsubscribe_url,
    )
    context = ssl.create_default_context()
    try:
        server: smtplib.SMTP = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        try:
            server.ehlo()
            if cfg["port"] == 587:
                server.starttls(context=context)
                server.ehlo()
            if cfg["user"] and cfg["password"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("SMTP send to %s failed: %s", to_email, exc)
        raise RuntimeError(f"SMTP send failed: {exc}") from exc

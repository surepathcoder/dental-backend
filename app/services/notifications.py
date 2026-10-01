"""Email (Gmail SMTP / Resend) and SMS (Twilio) delivery.

In DEMO_MODE, or when keys are missing, nothing is sent: the message is
recorded as SIMULATED so the automation can still be demonstrated end-to-end.
Gmail is the exception: if GMAIL_ADDRESS + GMAIL_APP_PASSWORD are set, email
is sent for real so local follow-ups can land in an inbox.
"""

from __future__ import annotations

import html
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger("brightsmile.notifications")


@dataclass
class DeliveryResult:
    status: str  # SENT | SIMULATED | FAILED
    detail: str = ""
    external_id: str | None = None


def _html_body(body_text: str) -> str:
    return (
        "<div style=\"font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#0b2233\">"
        + "".join(f"<p>{html.escape(p)}</p>" for p in body_text.split("\n\n"))
        + "</div>"
    )


def send_email(to: str, subject: str, body_text: str, ics: bytes | None = None) -> DeliveryResult:
    s = get_settings()
    if not s.email_live:
        logger.info("[SIMULATED EMAIL] to=%s subject=%s", to, subject)
        return DeliveryResult("SIMULATED", "Demo mode — email not sent")
    if s.gmail_ready:
        return _send_gmail(s, to, subject, body_text, ics=ics)
    return _send_resend(s, to, subject, body_text, ics=ics)


def appointment_ics(*, clinic_name: str, service: str, start_at, end_at, address: str, description: str = "", uid: str = "") -> bytes:
    """UTC VEVENT so phones can add the visit to a calendar."""

    def stamp(dt) -> str:
        return dt.replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")

    uid = uid or f"{service}-{stamp(start_at)}@practice"
    summary = f"{service} — {clinic_name}".replace("\n", " ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Practice//AI Receptionist//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp(start_at)}",
        f"DTSTART:{stamp(start_at)}",
        f"DTEND:{stamp(end_at)}",
        f"SUMMARY:{summary}",
        f"LOCATION:{address.replace(chr(10), ' ')}",
        f"DESCRIPTION:{description.replace(chr(10), ' ')}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines).encode("utf-8")


def _attach_ics(msg: EmailMessage, ics: bytes | None) -> None:
    if not ics:
        return
    msg.add_attachment(ics, maintype="text", subtype="calendar", filename="appointment.ics")


def _send_gmail(s: Any, to: str, subject: str, body_text: str, ics: bytes | None = None) -> DeliveryResult:
    password = s.gmail_app_password.replace(" ", "")
    from_addr = s.gmail_address.strip()
    msg = EmailMessage()
    msg["From"] = f"{s.gmail_from_name} <{from_addr}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)
    msg.add_alternative(_html_body(body_text), subtype="html")
    _attach_ics(msg, ics)
    try:
        with smtplib.SMTP(s.gmail_smtp_host, s.gmail_smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(from_addr, password)
            smtp.send_message(msg)
        logger.info("Gmail SMTP sent to=%s subject=%s", to, subject)
        return DeliveryResult("SENT", "Delivered via Gmail SMTP")
    except smtplib.SMTPAuthenticationError:
        logger.warning("Gmail SMTP auth failed for %s", from_addr)
        return DeliveryResult(
            "FAILED",
            "Gmail rejected the login. Use an App Password (Google Account → Security → App passwords), not your normal password.",
        )
    except Exception as exc:
        logger.warning("Gmail SMTP failed: %s", exc)
        return DeliveryResult("FAILED", str(exc)[:300])


def _send_resend(s: Any, to: str, subject: str, body_text: str, ics: bytes | None = None) -> DeliveryResult:
    payload: dict[str, Any] = {
        "from": s.resend_from_email,
        "to": [to],
        "subject": subject,
        "html": _html_body(body_text),
        "text": body_text,
    }
    if ics:
        import base64

        payload["attachments"] = [
            {"filename": "appointment.ics", "content": base64.b64encode(ics).decode("ascii"), "content_type": "text/calendar"}
        ]
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {s.resend_api_key}"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return DeliveryResult("SENT", "Delivered to Resend", resp.json().get("id"))
    except Exception as exc:
        logger.warning("Resend email failed: %s", exc)
        return DeliveryResult("FAILED", str(exc)[:300])


def to_e164(us_phone: str) -> str:
    digits = "".join(ch for ch in us_phone if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def send_sms(to: str, body: str) -> DeliveryResult:
    s = get_settings()
    if not s.sms_live:
        logger.info("[SIMULATED SMS] to=%s body=%s", to, body[:80])
        return DeliveryResult("SIMULATED", "Demo mode — SMS not sent")
    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
            auth=(s.twilio_account_sid, s.twilio_auth_token),
            data={"To": to_e164(to), "From": s.twilio_phone_number, "Body": body},
            timeout=10,
        )
        resp.raise_for_status()
        return DeliveryResult("SENT", "Queued with Twilio", resp.json().get("sid"))
    except Exception as exc:
        logger.warning("Twilio SMS failed: %s", exc)
        return DeliveryResult("FAILED", str(exc)[:300])

"""Gmail SMTP delivery — never hits a real mailbox in tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import smtplib

from app.services.notifications import _send_gmail


def _settings(**overrides):
    base = dict(
        gmail_address="clinic@gmail.com",
        gmail_app_password="abcd efgh ijkl mnop",
        gmail_from_name="BrightSmile Dental",
        gmail_smtp_host="smtp.gmail.com",
        gmail_smtp_port=587,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_gmail_smtp_sends_message():
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.__exit__.return_value = False
    with patch("app.services.notifications.smtplib.SMTP", return_value=smtp) as ctor:
        result = _send_gmail(_settings(), "patient@example.com", "You're booked", "See you Tuesday.")
    assert result.status == "SENT"
    ctor.assert_called_once_with("smtp.gmail.com", 587, timeout=15)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("clinic@gmail.com", "abcdefghijklmnop")
    smtp.send_message.assert_called_once()
    msg = smtp.send_message.call_args[0][0]
    assert msg["To"] == "patient@example.com"
    assert msg["Subject"] == "You're booked"


def test_gmail_smtp_can_attach_ics():
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.__exit__.return_value = False
    from datetime import datetime

    from app.services.notifications import appointment_ics

    ics = appointment_ics(
        clinic_name="Test Dental",
        service="Cleaning",
        start_at=datetime(2026, 9, 23, 15, 0, 0),
        end_at=datetime(2026, 9, 23, 16, 0, 0),
        address="1 Main St",
    )
    with patch("app.services.notifications.smtplib.SMTP", return_value=smtp):
        result = _send_gmail(_settings(), "patient@example.com", "Booked", "See you Tuesday.", ics=ics)
    assert result.status == "SENT"
    msg = smtp.send_message.call_args[0][0]
    raw = msg.as_string()
    assert "appointment.ics" in raw
    assert "text/calendar" in raw.lower()


def test_gmail_smtp_auth_error_is_failed():
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.__exit__.return_value = False
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
    with patch("app.services.notifications.smtplib.SMTP", return_value=smtp):
        result = _send_gmail(_settings(), "patient@example.com", "Hi", "Body")
    assert result.status == "FAILED"
    assert "App Password" in result.detail

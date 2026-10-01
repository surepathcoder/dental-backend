"""Admin-pasted API keys overlay environment variables at runtime.

Secrets live on Integration.config (and/or .env). HTTP responses only ever
include a masked hint, never the raw value.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import set_runtime_overrides
from ..models import Integration, utcnow

PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {
        "name": "Groq (LLM)",
        "category": "AI",
        "fields": [
            {"key": "GROQ_API_KEY", "field": "groq_api_key", "label": "API key", "secret": True, "placeholder": "gsk_..."},
            {"key": "GROQ_MODEL", "field": "groq_model", "label": "Model", "secret": False, "placeholder": "llama-3.3-70b-versatile"},
        ],
    },
    "openai": {
        "name": "OpenAI",
        "category": "AI",
        "fields": [
            {"key": "OPENAI_API_KEY", "field": "openai_api_key", "label": "API key", "secret": True, "placeholder": "sk-..."},
            {"key": "OPENAI_MODEL", "field": "openai_model", "label": "Model", "secret": False, "placeholder": "gpt-4o-mini"},
        ],
    },
    "twilio": {
        "name": "Twilio",
        "category": "SMS",
        "fields": [
            {"key": "TWILIO_ACCOUNT_SID", "field": "twilio_account_sid", "label": "Account SID", "secret": True, "placeholder": "AC..."},
            {"key": "TWILIO_AUTH_TOKEN", "field": "twilio_auth_token", "label": "Auth token", "secret": True, "placeholder": ""},
            {"key": "TWILIO_PHONE_NUMBER", "field": "twilio_phone_number", "label": "From number", "secret": False, "placeholder": "+1..."},
        ],
    },
    "gmail": {
        "name": "Gmail",
        "category": "Email",
        "fields": [
            {"key": "GMAIL_ADDRESS", "field": "gmail_address", "label": "Gmail address", "secret": False, "placeholder": "clinic@gmail.com"},
            {"key": "GMAIL_APP_PASSWORD", "field": "gmail_app_password", "label": "App password", "secret": True, "placeholder": "16-character app password"},
            {"key": "GMAIL_FROM_NAME", "field": "gmail_from_name", "label": "From name", "secret": False, "placeholder": "BrightSmile Dental"},
        ],
    },
    "resend": {
        "name": "Resend",
        "category": "Email",
        "fields": [
            {"key": "RESEND_API_KEY", "field": "resend_api_key", "label": "API key", "secret": True, "placeholder": "re_..."},
            {"key": "RESEND_FROM_EMAIL", "field": "resend_from_email", "label": "From email", "secret": False, "placeholder": "Clinic <hello@clinic.com>"},
        ],
    },
    "calcom": {
        "name": "Cal.com",
        "category": "Scheduling",
        "fields": [
            {"key": "CAL_API_KEY", "field": "cal_api_key", "label": "API key", "secret": True, "placeholder": "cal_live_..."},
            {"key": "CAL_EVENT_TYPE_ID", "field": "cal_event_type_id", "label": "Event type ID", "secret": False, "placeholder": ""},
        ],
    },
}

APP_PROVIDER = "app"


def mask_secret(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "••••"
    return f"••••{text[-4:]}"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _ensure_row(db: Session, clinic_id: int, provider: str) -> Integration:
    row = db.scalar(select(Integration).where(Integration.provider == provider))
    if row is None:
        row = Integration(clinic_id=clinic_id, provider=provider, enabled=True, config={})
        db.add(row)
        db.flush()
    return row


def collect_overrides(db: Session) -> dict[str, Any]:
    update: dict[str, Any] = {}
    for row in db.scalars(select(Integration)).all():
        cfg = row.config or {}
        if row.provider == APP_PROVIDER:
            if "DEMO_MODE" in cfg and cfg["DEMO_MODE"] not in (None, ""):
                update["demo_mode"] = _as_bool(cfg["DEMO_MODE"])
            continue
        spec = PROVIDERS.get(row.provider)
        if not spec:
            continue
        for field in spec["fields"]:
            val = cfg.get(field["key"])
            if val in (None, ""):
                continue
            update[field["field"]] = str(val).strip()
    return update


def apply_overrides(db: Session) -> None:
    set_runtime_overrides(collect_overrides(db))


def save_provider_values(db: Session, clinic_id: int, provider: str, values: dict[str, Any]) -> Integration:
    if provider == APP_PROVIDER:
        row = _ensure_row(db, clinic_id, APP_PROVIDER)
        cfg = dict(row.config or {})
        if "demo_mode" in values and values["demo_mode"] is not None:
            cfg["DEMO_MODE"] = "true" if _as_bool(values["demo_mode"]) else "false"
        row.config = cfg
        row.last_checked_at = utcnow()
        return row

    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"Unknown provider '{provider}'")
    allowed = {f["key"]: f for f in spec["fields"]}
    row = _ensure_row(db, clinic_id, provider)
    cfg = dict(row.config or {})
    for key, raw in values.items():
        field = allowed.get(key)
        if field is None:
            continue
        if raw is None:
            continue
        text = "" if isinstance(raw, bool) else str(raw).strip()
        if field["secret"] and text == "":
            # Blank secret means "keep the current value".
            continue
        if text.upper() == "CLEAR":
            cfg.pop(key, None)
            continue
        cfg[key] = text
    row.config = cfg
    row.last_checked_at = utcnow()
    return row


def configured_keys(row: Integration | None) -> set[str]:
    if row is None or not row.config:
        return set()
    return {k for k, v in row.config.items() if v not in (None, "")}

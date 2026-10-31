from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Integration, Service
from ..schemas import (
    ClinicOut,
    ClinicUpdate,
    IntegrationField,
    IntegrationStatus,
    IntegrationUpdate,
    RuntimeUpdate,
)
from ..security import audit, client_ip, rate_limit, require_admin
from ..services import llm
from ..services.runtime_settings import (
    APP_PROVIDER,
    PROVIDERS,
    apply_overrides,
    configured_keys,
    mask_secret,
    save_provider_values,
)

router = APIRouter(tags=["Clinic & Settings"])

EDITABLE_SETTING_GROUPS = {"assistant", "appointments", "sms", "email", "brand"}


def _sanitize_brand(values: dict) -> dict:
    out: dict = {}
    if "google_review_url" in values:
        url = str(values.get("google_review_url") or "").strip()[:400]
        if url and not (url.startswith("https://") or url.startswith("http://")):
            raise HTTPException(422, "google_review_url must start with https://")
        out["google_review_url"] = url
    if "production_per_visit" in values:
        try:
            amount = float(values.get("production_per_visit") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "production_per_visit must be a number") from exc
        if not 0 <= amount <= 50_000:
            raise HTTPException(422, "production_per_visit must be between 0 and 50000")
        out["production_per_visit"] = amount
    unknown = set(values) - {"google_review_url", "production_per_visit", "primary_color"}
    if unknown:
        raise HTTPException(422, f"Unknown brand fields: {', '.join(sorted(unknown))}")
    if "primary_color" in values:
        color = str(values.get("primary_color") or "").strip()
        if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise HTTPException(422, "primary_color must be a hex color like #0f6b70")
        out["primary_color"] = color
    return out


@router.get("/api/clinic", response_model=ClinicOut, dependencies=[Depends(rate_limit("clinic", 60))], summary="Clinic profile, services and FAQs")
def get_clinic(db: Session = Depends(get_db)) -> ClinicOut:
    return ClinicOut.model_validate(load_clinic(db))


@router.post("/api/clinic", response_model=ClinicOut, dependencies=[Depends(require_admin)], summary="Update clinic profile & settings")
def update_clinic(body: ClinicUpdate, request: Request, db: Session = Depends(get_db)) -> ClinicOut:
    clinic = load_clinic(db)
    data = body.model_dump(exclude_none=True, exclude={"settings", "services", "financing"})
    for key, value in data.items():
        setattr(clinic, key, value)
    if body.financing is not None:
        clinic.financing = {**(clinic.financing or {}), **body.financing}
    if body.settings is not None:
        unknown = set(body.settings) - EDITABLE_SETTING_GROUPS
        if unknown:
            raise HTTPException(422, f"Unknown settings groups: {', '.join(sorted(unknown))}")
        merged = dict(clinic.settings or {})
        for group, values in body.settings.items():
            if not isinstance(values, dict):
                raise HTTPException(422, f"settings.{group} must be an object")
            if group == "brand":
                values = _sanitize_brand(values)
            merged[group] = {**merged.get(group, {}), **values}
        clinic.settings = merged
    if body.services:
        by_id = {s.id: s for s in clinic.services}
        for patch in body.services:
            if patch.id is None:
                if not patch.name:
                    raise HTTPException(422, "A new service needs a name")
                slug = patch.slug or re.sub(r"[^a-z0-9]+", "-", patch.name.lower()).strip("-")[:80]
                svc = Service(clinic_id=clinic.id, slug=slug, name=patch.name, sort_order=len(clinic.services))
                clinic.services.append(svc)
            else:
                svc = by_id.get(patch.id)
            if svc is None:
                raise HTTPException(404, f"Service {patch.id} not found")
            for field, value in patch.model_dump(exclude_none=True, exclude={"id"}).items():
                setattr(svc, field, value)
    audit(db, "admin", "clinic.updated", "clinic", clinic.id, ip=client_ip(request),
          detail={"fields": list(data) + (["settings"] if body.settings else []) + (["services"] if body.services else []) + (["financing"] if body.financing else [])})
    db.commit()
    db.refresh(clinic)
    return ClinicOut.model_validate(clinic)


def _messaging_status(s, has_keys: bool, live: bool, provider_label: str) -> tuple[str, str]:
    if live:
        return "connected", f"{provider_label} is live."
    if has_keys and s.demo_mode:
        return "demo", "Keys saved, but Demo mode is on — messages are simulated."
    if s.demo_mode:
        return "demo", "No keys — simulated in demo mode."
    return "not_connected", "Paste the API keys below to enable."


def _field_payloads(provider: str, row: Integration | None) -> tuple[list[IntegrationField], str]:
    spec = PROVIDERS[provider]
    env_only = Settings()
    effective = get_settings()
    admin_keys = configured_keys(row)
    env_hits = False
    admin_hits = False
    fields: list[IntegrationField] = []
    for spec_field in spec["fields"]:
        env_val = str(getattr(env_only, spec_field["field"], "") or "").strip()
        eff_val = str(getattr(effective, spec_field["field"], "") or "").strip()
        in_admin = spec_field["key"] in admin_keys
        if in_admin:
            admin_hits = True
        if env_val:
            env_hits = True
        configured = bool(eff_val)
        fields.append(
            IntegrationField(
                key=spec_field["key"],
                label=spec_field["label"],
                secret=spec_field["secret"],
                configured=configured,
                hint=mask_secret(eff_val) if spec_field["secret"] and configured else None,
                value=None if spec_field["secret"] else (eff_val or None),
                placeholder=spec_field.get("placeholder") or "",
            )
        )
    if admin_hits and env_hits:
        source = "both"
    elif admin_hits:
        source = "admin"
    elif env_hits:
        source = "env"
    else:
        source = "none"
    return fields, source


def _integration_list(db: Session) -> list[IntegrationStatus]:
    s = get_settings()
    rows = {r.provider: r for r in db.scalars(select(Integration)).all()}

    ai_status = "connected" if s.llm_provider != "mock" else ("demo" if s.demo_mode else "not_connected")
    twilio_keys = bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_phone_number)
    tw_status, tw_detail = _messaging_status(s, twilio_keys, s.sms_live, "Twilio SMS")
    if s.gmail_ready:
        gm_status, gm_detail = "connected", f"Sending from {s.gmail_address} via Gmail SMTP."
    elif s.demo_mode:
        gm_status, gm_detail = "demo", "Add a Gmail address and App Password to send real emails."
    else:
        gm_status, gm_detail = "not_connected", "Add a Gmail address and App Password to enable."
    rs_status, rs_detail = _messaging_status(
        s, bool(s.resend_api_key), bool(s.resend_api_key) and s.email_live and not s.gmail_ready, "Resend email"
    )
    cal_keys = bool(s.cal_api_key and s.cal_event_type_id)
    cal_status, cal_detail = _messaging_status(s, cal_keys, s.calendar_live, "Cal.com scheduling")
    if cal_status != "connected":
        cal_detail += " Using the built-in scheduler until Cal.com is connected."

    groq_fields, groq_src = _field_payloads("groq", rows.get("groq"))
    openai_fields, openai_src = _field_payloads("openai", rows.get("openai"))
    twilio_fields, twilio_src = _field_payloads("twilio", rows.get("twilio"))
    gmail_fields, gmail_src = _field_payloads("gmail", rows.get("gmail"))
    resend_fields, resend_src = _field_payloads("resend", rows.get("resend"))
    cal_fields, cal_src = _field_payloads("calcom", rows.get("calcom"))

    return [
        IntegrationStatus(
            provider="groq", name="Groq (LLM)", category="AI",
            status="connected" if s.groq_api_key else ai_status if s.llm_provider == "mock" else "not_connected",
            detail=f"Model: {s.groq_model}" if s.groq_api_key else "Rule-based answers from the knowledge base." if s.llm_provider == "mock" else "Not configured — using OpenAI.",
            env_vars=["GROQ_API_KEY", "GROQ_MODEL"], fields=groq_fields, configured_in=groq_src,
        ),
        IntegrationStatus(
            provider="openai", name="OpenAI", category="AI",
            status="connected" if s.openai_api_key else ai_status if s.llm_provider == "mock" else "not_connected",
            detail=(f"Model: {s.openai_model}" + (" (standby — Groq is primary)" if s.groq_api_key else "")) if s.openai_api_key else "Optional alternative LLM provider.",
            env_vars=["OPENAI_API_KEY", "OPENAI_MODEL"], fields=openai_fields, configured_in=openai_src,
        ),
        IntegrationStatus(provider="twilio", name="Twilio", category="SMS", status=tw_status, detail=tw_detail,
                          env_vars=["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"],
                          fields=twilio_fields, configured_in=twilio_src),
        IntegrationStatus(provider="gmail", name="Gmail", category="Email", status=gm_status, detail=gm_detail,
                          env_vars=["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"], fields=gmail_fields, configured_in=gmail_src),
        IntegrationStatus(provider="resend", name="Resend", category="Email", status=rs_status, detail=rs_detail,
                          env_vars=["RESEND_API_KEY", "RESEND_FROM_EMAIL"], fields=resend_fields, configured_in=resend_src),
        IntegrationStatus(provider="calcom", name="Cal.com", category="Scheduling", status=cal_status, detail=cal_detail,
                          env_vars=["CAL_API_KEY", "CAL_EVENT_TYPE_ID"], fields=cal_fields, configured_in=cal_src),
    ]


@router.get("/api/integrations", response_model=list[IntegrationStatus], dependencies=[Depends(require_admin)],
            summary="Integration status (never returns secrets)")
def integrations(db: Session = Depends(get_db)) -> list[IntegrationStatus]:
    return _integration_list(db)


@router.post("/api/integrations/{provider}", response_model=list[IntegrationStatus], dependencies=[Depends(require_admin)],
             summary="Save API keys for a provider (never echoed back)")
def save_integration(provider: str, body: IntegrationUpdate, request: Request, db: Session = Depends(get_db)) -> list[IntegrationStatus]:
    if provider not in PROVIDERS:
        raise HTTPException(404, "Unknown integration")
    clinic = load_clinic(db)
    save_provider_values(db, clinic.id, provider, body.values)
    apply_overrides(db)
    audit(db, "admin", "integration.updated", "integration", provider, ip=client_ip(request),
          detail={"keys": sorted(k for k, v in body.values.items() if v not in (None, ""))})
    db.commit()
    return _integration_list(db)


@router.post("/api/runtime", dependencies=[Depends(require_admin)], summary="Toggle production / demo mode")
def update_runtime(body: RuntimeUpdate, request: Request, db: Session = Depends(get_db)) -> dict:
    clinic = load_clinic(db)
    save_provider_values(db, clinic.id, APP_PROVIDER, {"demo_mode": body.demo_mode})
    apply_overrides(db)
    audit(db, "admin", "runtime.updated", "settings", "demo_mode", ip=client_ip(request),
          detail={"demo_mode": get_settings().demo_mode})
    db.commit()
    s = get_settings()
    return {"demo_mode": s.demo_mode, "ai_provider": llm.provider_label(), "email": s.email_provider if s.email_live else "simulated",
            "sms": "twilio" if s.sms_live else "simulated", "scheduler": "calcom" if s.calendar_live else "mock"}


@router.get("/api/status", tags=["System"], summary="Public runtime status (demo flags, providers)")
def status() -> dict:
    s = get_settings()
    return {
        "demo_mode": s.demo_mode,
        "ai_provider": llm.provider_label(),
        "scheduler": "calcom" if s.calendar_live else "mock",
        "email": s.email_provider if s.email_live else "simulated",
        "sms": "twilio" if s.sms_live else "simulated",
        "database": "sqlite" if s.is_sqlite else "postgresql",
        "chat_session_ttl_minutes": s.chat_session_ttl_minutes,
    }

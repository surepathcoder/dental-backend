from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..database import get_db
from ..models import Appointment, Automation, AutomationEvent, Lead
from ..schemas import AutomationEventOut, AutomationOut, AutomationUpsert
from ..security import audit, client_ip, require_admin

router = APIRouter(prefix="/api/automations", tags=["Automations"], dependencies=[Depends(require_admin)])

ALLOWED_CONFIG_KEYS = {
    "missed_lead_followup": {"delay_hours", "template", "cadence_hours"},
    "sms_reminder": {"reminder_hours"},
    "review_request": {"delay_hours"},
    "no_show_recovery": {"delay_hours"},
    "hygiene_recall": {"delay_days"},
    "treatment_plan_followup": {"cadence_days", "template"},
    "staff_handoff_alert": set(),
    "appointment_confirmation": {"channels"},
    "lead_qualification": {"required_fields"},
    "ai_receptionist": {"after_hours_only"},
}


@router.get("", response_model=list[AutomationOut], summary="List automations")
def list_automations(db: Session = Depends(get_db)) -> list[Automation]:
    return list(db.scalars(select(Automation).order_by(Automation.id)).all())


@router.get("/events", response_model=list[AutomationEventOut], summary="Automation activity log")
def list_events(
    db: Session = Depends(get_db),
    key: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AutomationEvent]:
    live_leads = select(Lead.id).where(Lead.is_demo.is_(False))
    live_appts = select(Appointment.id).where(Appointment.is_demo.is_(False))
    stmt = select(AutomationEvent).where(
        or_(AutomationEvent.lead_id.in_(live_leads), AutomationEvent.appointment_id.in_(live_appts))
    )
    if key:
        stmt = stmt.where(AutomationEvent.automation_key == key)
    return list(db.scalars(stmt.order_by(AutomationEvent.created_at.desc(), AutomationEvent.id.desc()).limit(limit)).all())


def _validate_config(key: str, config: dict) -> dict:
    allowed = ALLOWED_CONFIG_KEYS.get(key)
    if allowed is not None:
        unknown = set(config) - allowed
        if unknown:
            raise HTTPException(422, f"Unsupported config keys for {key}: {', '.join(sorted(unknown))}")
    if "delay_hours" in config:
        try:
            v = float(config["delay_hours"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "delay_hours must be a number") from exc
        if not 0 <= v <= 24 * 14:
            raise HTTPException(422, "delay_hours must be between 0 and 336")
        config["delay_hours"] = v
    if "reminder_hours" in config:
        hours = config["reminder_hours"]
        if not isinstance(hours, list) or not all(isinstance(h, (int, float)) and 0 < h <= 168 for h in hours) or len(hours) > 4:
            raise HTTPException(422, "reminder_hours must be a list of up to 4 numbers between 0 and 168")
    if "cadence_hours" in config:
        hours = config["cadence_hours"]
        if not isinstance(hours, list) or not all(isinstance(h, (int, float)) and 0 < h <= 168 for h in hours) or not 1 <= len(hours) <= 4:
            raise HTTPException(422, "cadence_hours must be a list of 1–4 numbers between 0 and 168")
        config["cadence_hours"] = [float(h) for h in hours]
    if "delay_days" in config:
        try:
            days = float(config["delay_days"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "delay_days must be a number") from exc
        if not 0 <= days <= 730:
            raise HTTPException(422, "delay_days must be between 0 and 730")
        config["delay_days"] = days
    if "cadence_days" in config:
        days_list = config["cadence_days"]
        if not isinstance(days_list, list) or not all(isinstance(d, (int, float)) and 0 < d <= 90 for d in days_list) or not 1 <= len(days_list) <= 4:
            raise HTTPException(422, "cadence_days must be a list of 1–4 numbers between 0 and 90")
        config["cadence_days"] = [float(d) for d in days_list]
    if "template" in config and (not isinstance(config["template"], str) or len(config["template"]) > 480):
        raise HTTPException(422, "template must be a string of at most 480 characters")
    return config


@router.post("", response_model=AutomationOut, summary="Create or update an automation (status, config, steps)")
def upsert_automation(body: AutomationUpsert, request: Request, db: Session = Depends(get_db)) -> Automation:
    auto = db.scalar(select(Automation).where(Automation.key == body.key))
    if auto is None:
        if not (body.name and body.trigger):
            raise HTTPException(422, "name and trigger are required to create an automation")
        auto = Automation(clinic_id=load_clinic(db).id, key=body.key, name=body.name, trigger=body.trigger,
                          description=body.description or "", steps=body.steps or [], status=body.status or "ACTIVE", config={})
        db.add(auto)
    else:
        for field in ("name", "description", "trigger", "steps", "status"):
            value = getattr(body, field)
            if value is not None:
                setattr(auto, field, value)
    if body.config is not None:
        auto.config = {**(auto.config or {}), **_validate_config(body.key, dict(body.config))}
    audit(db, "admin", "automation.updated", "automation", body.key, ip=client_ip(request),
          detail={"status": body.status, "config_keys": list((body.config or {}).keys())})
    db.commit()
    db.refresh(auto)
    return auto

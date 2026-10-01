from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..agent.runner import load_clinic
from ..database import get_db
from ..models import FollowUp, Lead, utcnow
from ..schemas import FollowUpCreate, FollowUpOut
from ..security import audit, client_ip, require_admin
from ..services import automations
from ..services.leads import first_name

router = APIRouter(prefix="/api/followups", tags=["Follow-ups"], dependencies=[Depends(require_admin)])


def _out(fu: FollowUp) -> FollowUpOut:
    return FollowUpOut.model_validate(fu).model_copy(update={"lead_name": fu.lead.name if fu.lead else None})


@router.get("", response_model=list[FollowUpOut], summary="List follow-ups")
def list_followups(
    db: Session = Depends(get_db),
    kind: str | None = Query(default=None, max_length=40),
    status: str | None = Query(default=None, pattern=r"^(SCHEDULED|SENT|SIMULATED|CANCELLED|FAILED)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[FollowUpOut]:
    stmt = select(FollowUp).options(selectinload(FollowUp.lead)).join(Lead).where(Lead.is_demo.is_(False))
    if kind:
        stmt = stmt.where(FollowUp.kind == kind)
    if status:
        stmt = stmt.where(FollowUp.status == status)
    return [_out(f) for f in db.scalars(stmt.order_by(FollowUp.due_at.desc()).limit(limit)).all()]


@router.post("", response_model=FollowUpOut, status_code=201, summary="Schedule a follow-up for a lead")
def create_followup(body: FollowUpCreate, request: Request, db: Session = Depends(get_db)) -> FollowUpOut:
    lead = db.get(Lead, body.lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found")
    clinic = load_clinic(db)
    if body.kind == "missed_lead" and body.message is None:
        fu, _ = automations.schedule_missed_lead_followup(db, clinic, lead, "Scheduled manually by staff")
        if fu is None:
            raise HTTPException(422, "Lead needs a phone or email, must not be booked/lost, and the automation must be active.")
        if body.delay_hours != 24:
            fu.due_at = utcnow() + timedelta(hours=body.delay_hours)
    elif body.kind == "treatment_plan" and body.message is None:
        events = automations.schedule_treatment_plan_followup(db, clinic, lead)
        if not events:
            raise HTTPException(422, "Lead needs a phone or email, and treatment-plan follow-up must be active with none already scheduled.")
        fu = db.scalars(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind.like("treatment_plan%")).order_by(FollowUp.due_at)).first()
        if fu is None:
            raise HTTPException(422, "Could not schedule treatment-plan follow-up.")
    else:
        channel = body.channel or ("email" if lead.email else "sms")
        recipient = lead.email if channel == "email" else lead.phone
        if not recipient:
            raise HTTPException(422, f"Lead has no {'phone' if channel == 'sms' else 'email'} on file")
        fu = FollowUp(lead_id=lead.id, kind=body.kind, channel=channel, recipient=recipient,
                      message=body.message or f"Hi {first_name(lead)}, this is {clinic.name}. Just checking in — can we help you find a time?",
                      due_at=utcnow() + timedelta(hours=body.delay_hours), status="SCHEDULED")
        db.add(fu)
    audit(db, "admin", "followup.created", "lead", lead.id, ip=client_ip(request), detail={"kind": body.kind})
    db.commit()
    db.refresh(fu)
    return _out(fu)


@router.post("/{followup_id}/send", response_model=FollowUpOut, summary="Send a scheduled follow-up now")
def send_now(followup_id: int, request: Request, db: Session = Depends(get_db)) -> FollowUpOut:
    fu = db.get(FollowUp, followup_id)
    if fu is None:
        raise HTTPException(404, "Follow-up not found")
    if fu.status != "SCHEDULED":
        raise HTTPException(409, f"Follow-up is already {fu.status.lower()}")
    automations.deliver_followup(db, fu)
    audit(db, "admin", "followup.sent", "followup", fu.id, ip=client_ip(request))
    db.commit()
    db.refresh(fu)
    return _out(fu)


@router.post("/process", summary="Deliver all follow-ups that are due")
def process_due(db: Session = Depends(get_db)) -> dict:
    sent = automations.process_due_followups(db)
    db.commit()
    return {"processed": sent}

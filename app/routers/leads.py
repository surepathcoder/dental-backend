from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Appointment, AutomationEvent, Clinic, Conversation, FollowUp, Lead
from ..schemas import AppointmentOut, AutomationEventOut, ConversationDetail, FollowUpOut, LeadCreate, LeadDetail, LeadOut, LeadStatus, LeadUpdate, MessageOut
from ..security import audit, client_ip, rate_limit, require_admin
from ..services import automations
from ..services.leads import refresh_lead

router = APIRouter(prefix="/api/leads", tags=["Leads"])


@router.post("", response_model=LeadOut, status_code=201, dependencies=[Depends(rate_limit("lead_create", 10))],
             summary="Create a lead (public contact form)")
def create_lead(body: LeadCreate, request: Request, db: Session = Depends(get_db)) -> Lead:
    clinic = db.scalar(select(Clinic))
    lead = None
    if body.email:
        lead = db.scalar(select(Lead).where(Lead.email == body.email).order_by(Lead.id.desc()))
    if lead is None:
        lead = Lead(clinic_id=clinic.id, status="NEW", source=body.source)
        db.add(lead)
    for field in ("name", "phone", "email", "service", "patient_type", "insurance", "urgency", "preferred_date", "preferred_time"):
        value = getattr(body, field)
        if value:
            setattr(lead, field, value)
    if body.message:
        lead.notes = ((lead.notes or "") + f"\nContact form: {body.message}").strip()
    refresh_lead(lead)
    db.flush()
    automations.record(db, "lead_qualification", f"New lead from {body.source.replace('_', ' ')}: {lead.name}",
                       detail=lead.service or "General inquiry", lead_id=lead.id)
    audit(db, "public", "lead.created", "lead", lead.id, ip=client_ip(request), detail={"source": body.source})
    db.commit()
    return lead


@router.get("", response_model=dict, summary="List leads", dependencies=[Depends(require_admin)])
def list_leads(
    db: Session = Depends(get_db),
    status: LeadStatus | None = None,
    service: str | None = Query(default=None, max_length=120),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    stmt = select(Lead).where(Lead.is_demo.is_(False))
    if status:
        stmt = stmt.where(Lead.status == status)
    if service:
        stmt = stmt.where(Lead.service == service)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Lead.name).like(like), func.lower(Lead.email).like(like), Lead.phone.like(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Lead.last_activity_at.desc()).limit(limit).offset(offset)).all()
    counts = dict(db.execute(select(Lead.status, func.count(Lead.id)).where(Lead.is_demo.is_(False)).group_by(Lead.status)).all())
    return {"items": [LeadOut.model_validate(r).model_dump(mode="json") for r in rows], "total": total, "status_counts": counts}


@router.get("/{lead_id}", response_model=LeadDetail, summary="Lead detail with conversation history", dependencies=[Depends(require_admin)])
def get_lead(lead_id: int, db: Session = Depends(get_db)) -> LeadDetail:
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found")
    convos = db.scalars(select(Conversation).where(Conversation.lead_id == lead_id).options(selectinload(Conversation.messages)).order_by(Conversation.created_at)).all()
    appts = db.scalars(select(Appointment).where(Appointment.lead_id == lead_id).order_by(Appointment.start_at)).all()
    fus = db.scalars(select(FollowUp).where(FollowUp.lead_id == lead_id).order_by(FollowUp.due_at)).all()
    events = db.scalars(select(AutomationEvent).where(AutomationEvent.lead_id == lead_id).order_by(AutomationEvent.created_at)).all()
    base = LeadOut.model_validate(lead).model_dump()
    return LeadDetail(
        **base,
        conversations=[
            ConversationDetail.model_validate(c).model_copy(update={
                "patient_name": lead.name, "message_count": len(c.messages),
                "messages": [MessageOut.model_validate(m) for m in c.messages],
            }) for c in convos
        ],
        appointments=[AppointmentOut.model_validate(a) for a in appts],
        followups=[FollowUpOut.model_validate(f) for f in fus],
        events=[AutomationEventOut.model_validate(e) for e in events],
    )


@router.patch("/{lead_id}", response_model=LeadOut, summary="Update lead status / notes", dependencies=[Depends(require_admin)])
def update_lead(lead_id: int, body: LeadUpdate, request: Request, db: Session = Depends(get_db)) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found")
    changes = body.model_dump(exclude_none=True)
    if "status" in changes:
        lead.status = changes["status"]
    if "notes" in changes:
        lead.notes = changes["notes"]
    refresh_lead(lead)
    if lead.status in {"BOOKED", "CONVERTED", "LOST"}:
        for fu in db.scalars(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind == "missed_lead", FollowUp.status == "SCHEDULED")):
            fu.status = "CANCELLED"
    audit(db, "admin", "lead.updated", "lead", lead.id, ip=client_ip(request), detail={"fields": list(changes)})
    db.commit()
    return lead

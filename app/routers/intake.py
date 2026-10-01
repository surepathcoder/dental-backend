from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..database import get_db
from ..models import Appointment, IntakeSubmission
from ..schemas import IntakeOut, IntakePreview, IntakeSubmit
from ..security import audit, client_ip, rate_limit
from ..services.scheduler import format_day, format_time

router = APIRouter(prefix="/api/intake", tags=["Intake"])


@router.get("", response_model=IntakePreview, dependencies=[Depends(rate_limit("intake", 40))])
def preview_intake(appointment_id: int = Query(..., ge=1), db: Session = Depends(get_db)) -> IntakePreview:
    clinic = load_clinic(db)
    appt = db.get(Appointment, appointment_id)
    if appt is None or appt.clinic_id != clinic.id:
        raise HTTPException(404, "Appointment not found")
    local = appt.start_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(clinic.timezone))
    submitted = db.scalar(select(IntakeSubmission).where(IntakeSubmission.appointment_id == appt.id))
    first = (appt.patient_name or "there").split()[0]
    return IntakePreview(
        appointment_id=appt.id,
        clinic_name=clinic.name,
        patient_first_name=first,
        service=appt.service,
        date_label=format_day(local),
        time_label=format_time(local),
        already_submitted=submitted is not None,
    )


@router.post("", response_model=IntakeOut, status_code=201, dependencies=[Depends(rate_limit("intake_post", 8))])
def submit_intake(body: IntakeSubmit, request: Request, db: Session = Depends(get_db)) -> IntakeSubmission:
    clinic = load_clinic(db)
    appt = db.get(Appointment, body.appointment_id)
    if appt is None or appt.clinic_id != clinic.id:
        raise HTTPException(404, "Appointment not found")
    existing = db.scalar(select(IntakeSubmission).where(IntakeSubmission.appointment_id == appt.id))
    if existing:
        raise HTTPException(409, "Intake already submitted for this visit")
    row = IntakeSubmission(
        clinic_id=clinic.id,
        appointment_id=appt.id,
        name=body.name,
        phone=body.phone or appt.patient_phone,
        email=str(body.email) if body.email else appt.patient_email,
        answers={
            "reason": body.reason or "",
            "medications": body.medications or "",
            "allergies": body.allergies or "",
            "insurance": body.insurance or "",
        },
    )
    db.add(row)
    if "intake" not in (appt.notes or "").lower():
        appt.notes = ((appt.notes or "").rstrip() + "\nIntake form submitted.").strip()
    audit(db, "public", "intake.submitted", "appointment", appt.id, ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return row

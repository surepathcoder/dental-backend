from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..config import get_settings
from ..database import get_db
from ..models import Appointment, Lead
from ..schemas import AppointmentCreate, AppointmentOut, AppointmentUpdate, AvailabilityOut, BookingResult
from ..security import audit, client_ip, rate_limit, require_admin
from ..services import automations, scheduler
from ..services.leads import refresh_lead

router = APIRouter(prefix="/api/appointments", tags=["Appointments"])


@router.get("/availability", response_model=AvailabilityOut, dependencies=[Depends(rate_limit("availability", 60))],
            summary="Available appointment slots for a service")
def availability(
    db: Session = Depends(get_db),
    service: str = Query(default="General Dentistry", max_length=120),
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Local date (YYYY-MM-DD)"),
    limit: int = Query(default=12, ge=1, le=40),
) -> AvailabilityOut:
    clinic = load_clinic(db)
    valid = {s.name for s in clinic.services if s.is_active}
    if service not in valid:
        raise HTTPException(422, f"Unknown service. Choose one of: {', '.join(sorted(valid))}")
    pref = scheduler.Preference(earliest=True)
    if date:
        d = datetime.fromisoformat(date).date()
        pref = scheduler.Preference(start_date=d, end_date=d, earliest=True)
    slots = scheduler.get_availability(db, clinic, service, pref, limit=limit)
    return AvailabilityOut(service=service, timezone=clinic.timezone, scheduler=scheduler.scheduler_name(),
                           demo=get_settings().demo_mode or scheduler.scheduler_name() == "mock", slots=slots)


@router.post("", response_model=BookingResult, status_code=201, dependencies=[Depends(rate_limit("book", 8))],
             summary="Book an appointment (online booking form)")
def create_appointment(body: AppointmentCreate, request: Request, db: Session = Depends(get_db)) -> BookingResult:
    settings = get_settings()
    clinic = load_clinic(db)
    if body.service not in {s.name for s in clinic.services if s.is_active}:
        raise HTTPException(422, "Unknown service")
    try:
        start = scheduler.validate_slot(db, clinic, body.service, body.slot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    lead = db.scalar(select(Lead).where(Lead.email == body.email).order_by(Lead.id.desc()))
    if lead is None:
        lead = Lead(clinic_id=clinic.id, source=body.source, status="NEW", is_demo=False)
        db.add(lead)
    lead.name, lead.phone, lead.email, lead.service = body.name, body.phone, body.email, body.service
    lead.patient_type = body.patient_type
    lead.insurance = body.insurance or lead.insurance
    lead.status = "BOOKED"
    refresh_lead(lead)
    db.flush()

    sched = scheduler.scheduler_name()
    external_id = None
    if sched == "calcom":
        external_id = scheduler.calcom_book(clinic, start, body.name, body.email, body.phone, body.service)
        if not external_id:
            sched = "mock"
    appt = Appointment(
        clinic_id=clinic.id, lead_id=lead.id, patient_name=body.name, patient_phone=body.phone, patient_email=body.email,
        service=body.service, provider_name=scheduler.provider_for(body.service), start_at=start,
        end_at=start + timedelta(minutes=scheduler.duration_for(body.service)), status="CONFIRMED", source=body.source,
        scheduler=sched, external_id=external_id, is_demo=False, notes=body.notes or "",
    )
    db.add(appt)
    db.flush()
    events = automations.on_appointment_booked(db, clinic, lead, appt)
    audit(db, "public", "appointment.booked", "appointment", appt.id, ip=client_ip(request), detail={"service": body.service, "source": body.source})
    db.commit()
    return BookingResult(appointment=AppointmentOut.model_validate(appt), lead_id=lead.id, events=events, demo=settings.demo_mode or sched == "mock")


@router.get("", response_model=list[AppointmentOut], dependencies=[Depends(require_admin)], summary="List appointments")
def list_appointments(
    db: Session = Depends(get_db),
    start: str | None = Query(default=None, description="ISO date (local) — defaults to 14 days ago"),
    days: int = Query(default=45, ge=1, le=120),
    status: str | None = Query(default=None, pattern=r"^(CONFIRMED|COMPLETED|CANCELLED|NO_SHOW)$"),
) -> list[Appointment]:
    clinic = load_clinic(db)
    tz = ZoneInfo(clinic.timezone)
    start_local = datetime.fromisoformat(start).replace(tzinfo=tz) if start else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
    range_start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    stmt = select(Appointment).where(
        Appointment.is_demo.is_(False),
        Appointment.start_at >= range_start,
        Appointment.start_at < range_start + timedelta(days=days),
    )
    if status:
        stmt = stmt.where(Appointment.status == status)
    return list(db.scalars(stmt.order_by(Appointment.start_at)).all())


@router.patch("/{appointment_id}", response_model=AppointmentOut, dependencies=[Depends(require_admin)], summary="Update appointment status")
def update_appointment(appointment_id: int, body: AppointmentUpdate, request: Request, db: Session = Depends(get_db)) -> Appointment:
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise HTTPException(404, "Appointment not found")
    automations.apply_appointment_status(db, appt, body.status)
    audit(db, "admin", "appointment.updated", "appointment", appt.id, ip=client_ip(request), detail={"status": body.status})
    db.commit()
    return appt

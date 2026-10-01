"""Short-term (this chat session) and long-term (phone/email) patient memory.

Session memory lives on Conversation.state (stage + fields) and recent messages.
It is scoped to one conversation id and expires after ``chat_session_ttl_minutes``
of inactivity — a later visit starts a new session instead of resuming a stale
booking flow.

Long-term recall is derived from other leads and appointments that share a phone
or email once the visitor identifies themselves again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Appointment, Clinic, Conversation, Lead, utcnow
from ..services.scheduler import format_day, format_time


@dataclass
class PatientMemory:
    first_name: str | None = None
    returning: bool = False
    insurance: str | None = None
    last_service: str | None = None
    upcoming_summary: str | None = None
    upcoming_id: int | None = None


def session_expired(convo: Conversation) -> bool:
    """True when this chat's working memory should not be reused."""
    ttl = get_settings().chat_session_ttl_minutes
    if ttl <= 0 or not convo.last_message_at:
        return False
    return utcnow() - convo.last_message_at > timedelta(minutes=ttl)


def _first(name: str | None) -> str | None:
    parts = (name or "").split()
    return parts[0] if parts else None


def _local_summary(clinic: Clinic, appt: Appointment) -> str:
    local = appt.start_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(clinic.timezone))
    return (
        f"{appt.service} on {format_day(local)} at {format_time(local)} "
        f"with {appt.provider_name or 'our team'}"
    )


def working_memory_lines(fields: dict, stage: str, mem: PatientMemory | None = None) -> list[str]:
    """Compact facts for this visit — used as LLM working memory."""
    lines: list[str] = []
    if fields.get("name"):
        lines.append(f"name: {fields['name']}")
    if fields.get("service"):
        lines.append(f"service interest: {fields['service']}")
    if fields.get("patient_type"):
        lines.append(f"patient type: {fields['patient_type']}")
    if fields.get("insurance"):
        lines.append(f"insurance: {fields['insurance']}")
    if fields.get("urgency"):
        lines.append(f"urgency: {fields['urgency']}")
    if stage:
        lines.append(f"booking stage: {stage}")
    if mem and mem.upcoming_summary:
        lines.append(f"already booked: {mem.upcoming_summary}")
        lines.append("Do not offer to book another appointment unless they ask to add or reschedule.")
    return lines


def load_patient_memory(
    db: Session,
    clinic: Clinic,
    fields: dict,
    conversation: Conversation,
) -> PatientMemory:
    mem = PatientMemory(
        first_name=_first(fields.get("name")),
        insurance=fields.get("insurance"),
        last_service=fields.get("service"),
    )
    phone = fields.get("phone")
    email = (fields.get("email") or "").strip().lower() or None
    now = utcnow()

    upcoming_rows = list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.clinic_id == clinic.id,
                Appointment.status == "CONFIRMED",
                Appointment.start_at >= now,
            )
            .order_by(Appointment.start_at)
        ).all()
    )

    def matches(appt: Appointment) -> bool:
        if appt.conversation_id == conversation.id:
            return True
        if phone and appt.patient_phone == phone:
            return True
        if email and (appt.patient_email or "").lower() == email:
            return True
        return False

    this_convo = [a for a in upcoming_rows if a.conversation_id == conversation.id]
    identified = [a for a in upcoming_rows if matches(a)]
    chosen = (this_convo or identified or [None])[0]
    if chosen:
        mem.upcoming_id = chosen.id
        mem.upcoming_summary = _local_summary(clinic, chosen)
        mem.first_name = mem.first_name or _first(chosen.patient_name)

    if not (phone or email):
        return mem

    appt_clauses = []
    if phone:
        appt_clauses.append(Appointment.patient_phone == phone)
    if email:
        appt_clauses.append(Appointment.patient_email == email)
    other_appts = list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.clinic_id == clinic.id,
                Appointment.conversation_id != conversation.id,
                or_(*appt_clauses),
            )
            .order_by(Appointment.start_at.desc())
            .limit(8)
        ).all()
    )
    if other_appts:
        mem.returning = True
        mem.first_name = mem.first_name or _first(other_appts[0].patient_name)
        past = next((a for a in other_appts if a.start_at < now or a.status == "COMPLETED"), other_appts[0])
        mem.last_service = mem.last_service or past.service
        if not mem.upcoming_summary:
            future = next((a for a in other_appts if a.status == "CONFIRMED" and a.start_at >= now), None)
            if future:
                mem.upcoming_id = future.id
                mem.upcoming_summary = _local_summary(clinic, future)

    lead_clauses = []
    if phone:
        lead_clauses.append(Lead.phone == phone)
    if email:
        lead_clauses.append(Lead.email == email)
    other_lead = db.scalar(
        select(Lead).where(Lead.clinic_id == clinic.id, or_(*lead_clauses)).order_by(Lead.id.desc()).limit(1)
    )
    if other_lead and other_lead.id != conversation.lead_id:
        mem.first_name = mem.first_name or _first(other_lead.name)
        mem.insurance = mem.insurance or other_lead.insurance
        mem.last_service = mem.last_service or other_lead.service
        if other_lead.status in {"BOOKED", "CONVERTED", "FOLLOW_UP"}:
            mem.returning = True

    return mem

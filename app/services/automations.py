"""Automation engine: confirmations, reminders, missed-lead follow-ups and
review requests. Every step is logged as an AutomationEvent so the admin
Automation Center can show exactly what happened and when."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Appointment, Automation, AutomationEvent, Clinic, Conversation, FollowUp, Lead, WaitlistEntry, utcnow
from ..schemas import FlowEvent
from .leads import first_name, refresh_lead
from .notifications import DeliveryResult, appointment_ics, send_email, send_sms
from .scheduler import format_day, format_time, iso_z

logger = logging.getLogger("brightsmile.automations")


# ── helpers ──────────────────────────────────────────────────────────────────
def get_automation(db: Session, key: str) -> Automation | None:
    return db.scalar(select(Automation).where(Automation.key == key))


def is_active(db: Session, key: str) -> bool:
    auto = get_automation(db, key)
    return bool(auto and auto.status == "ACTIVE")


def record(
    db: Session,
    key: str,
    title: str,
    *,
    detail: str = "",
    status: str = "COMPLETED",
    lead_id: int | None = None,
    appointment_id: int | None = None,
    channel: str | None = None,
    count_run: bool = True,
) -> AutomationEvent:
    ev = AutomationEvent(
        automation_key=key,
        title=title,
        detail=detail,
        status=status,
        lead_id=lead_id,
        appointment_id=appointment_id,
        channel=channel,
    )
    db.add(ev)
    if count_run:
        auto = get_automation(db, key)
        if auto:
            auto.runs_count = (auto.runs_count or 0) + 1
            auto.last_run_at = utcnow()
    return ev


def _local(clinic: Clinic, dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(clinic.timezone))


def _fmt(template: str, **values: str) -> str:
    class Safe(dict):
        def __missing__(self, key: str) -> str:
            return ""

    return template.format_map(Safe(values))


def _flow_status(delivery: DeliveryResult) -> str:
    return {"SENT": "completed", "SIMULATED": "simulated"}.get(delivery.status, "failed")


def _site_url() -> str:
    return get_settings().public_site_url.rstrip("/")


def _booking_url() -> str:
    return f"{_site_url()}/book"


def _intake_url(appointment_id: int) -> str:
    return f"{_site_url()}/intake?appointment_id={appointment_id}"


def _review_url(clinic: Clinic) -> str:
    brand = (clinic.settings or {}).get("brand") or {}
    return str(brand.get("google_review_url") or "").strip()


def _template_values(clinic: Clinic, lead: Lead | None, appt: Appointment | None = None) -> dict[str, str]:
    local = _local(clinic, appt.start_at) if appt else None
    return {
        "first_name": first_name(lead) if lead else "there",
        "service": (appt.service if appt else None) or (lead.service if lead else None) or "a visit",
        "date": format_day(local) if local else "",
        "time": format_time(local) if local else "",
        "clinic": clinic.name,
        "phone": clinic.phone,
        "booking_url": _booking_url(),
        "intake_url": _intake_url(appt.id) if appt else f"{_site_url()}/intake",
        "review_url": _review_url(clinic) or "your Google listing",
    }


def preferred_channel(email: str | None, phone: str | None) -> tuple[str, str] | None:
    """Email first when an address is on file, otherwise SMS."""
    if email:
        return "email", email
    if phone:
        return "sms", phone
    return None


def reminder_kind_and_label(hours: float) -> tuple[str, str]:
    minutes = int(round(float(hours) * 60))
    if minutes % 60 == 0:
        h = max(minutes // 60, 1)
        return f"reminder_{h}h", f"{h}-hour reminder"
    return f"reminder_{minutes}m", f"{minutes}-minute reminder"


def reminder_when_phrase(hours: float) -> str:
    minutes = int(round(float(hours) * 60))
    if hours >= 24:
        days = int(round(hours / 24))
        return "tomorrow" if days <= 1 else f"in {days} days"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"in {h} hour" if h == 1 else f"in {h} hours"
    return f"in {minutes} minutes"


def _clock(local: datetime) -> str:
    abbr = (local.tzname() or "").strip()
    return f"{format_time(local)} {abbr}".strip()


def _relative_when(clinic: Clinic, due: datetime) -> str:
    local_due = _local(clinic, due)
    today = datetime.now(ZoneInfo(clinic.timezone)).date()
    clock = _clock(local_due)
    if local_due.date() == today:
        return f"today at {clock}"
    if local_due.date() == today + timedelta(days=1):
        return f"tomorrow at {clock}"
    return f"{format_day(local_due)} at {clock}"


REMINDER_LEAD_MINUTES = 20
NEAR_VISIT_HOURS = 12


def _planned_reminders(
    hours_list: list,
    start: datetime,
    now: datetime,
) -> tuple[list[tuple[float, datetime]], list[tuple[float, str]]]:
    """Pick reminder send times from the visit start.

    Skips anything that would fire immediately (confirmation already covers that)
    and collapses same-day visits to a single near-visit ping.
    """
    offsets: list[float] = []
    seen: set[float] = set()
    for raw in hours_list:
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            continue
        if hours <= 0 or hours in seen:
            continue
        seen.add(hours)
        offsets.append(hours)
    if not any(hours <= 1 for hours in offsets):
        offsets.append(0.5)
    offsets.sort(reverse=True)
    keep: list[tuple[float, datetime]] = []
    skipped: list[tuple[float, str]] = []
    lead = timedelta(minutes=REMINDER_LEAD_MINUTES)
    for hours in offsets:
        due = start - timedelta(hours=hours)
        if due <= now:
            skipped.append((hours, "Skipped — appointment is sooner than this"))
            continue
        if due <= now + lead:
            skipped.append((hours, "Skipped — would send right after confirmation"))
            continue
        keep.append((hours, due))
    hours_until = (start - now).total_seconds() / 3600
    if hours_until <= NEAR_VISIT_HOURS and len(keep) > 1:
        for hours, _due in keep[:-1]:
            skipped.append((hours, "Skipped — same-day visits get one reminder"))
        keep = [keep[-1]]
    return keep, skipped


# ── Booking confirmation + reminders ─────────────────────────────────────────
def apply_appointment_status(db: Session, appt: Appointment, status: str) -> None:
    """Shared cancel / complete / no-show side effects (reminders + lead status)."""
    appt.status = status
    lead = db.get(Lead, appt.lead_id) if appt.lead_id else None
    if status in {"CANCELLED", "NO_SHOW", "COMPLETED"}:
        for fu in db.scalars(
            select(FollowUp).where(
                FollowUp.appointment_id == appt.id,
                FollowUp.status == "SCHEDULED",
                FollowUp.kind.like("reminder%"),
            )
        ):
            fu.status = "CANCELLED"
    if status == "CANCELLED":
        for fu in db.scalars(
            select(FollowUp).where(
                FollowUp.appointment_id == appt.id,
                FollowUp.status == "SCHEDULED",
                FollowUp.kind == "hygiene_recall",
            )
        ):
            fu.status = "CANCELLED"
    if lead:
        if status == "COMPLETED":
            lead.status = "CONVERTED"
            clinic = db.get(Clinic, appt.clinic_id)
            if clinic:
                schedule_review_request(db, clinic, lead, appt)
                schedule_treatment_plan_followup(db, clinic, lead, appt)
        elif status == "CANCELLED":
            lead.status = "FOLLOW_UP"
            notify_waitlist(db, appt)
        elif status == "NO_SHOW":
            lead.status = "FOLLOW_UP"
            clinic = db.get(Clinic, appt.clinic_id)
            if clinic:
                schedule_no_show_recovery(db, clinic, lead, appt)
        refresh_lead(lead)


def on_appointment_booked(db: Session, clinic: Clinic, lead: Lead, appt: Appointment) -> list[FlowEvent]:
    """Full post-booking automation: confirmations + reminders + recall."""
    return send_confirmations(db, clinic, lead, appt) + schedule_reminders(db, clinic, lead, appt) + schedule_hygiene_recall(db, clinic, lead, appt)


def send_confirmations(db: Session, clinic: Clinic, lead: Lead, appt: Appointment) -> list[FlowEvent]:
    settings = clinic.settings or {}
    sms_cfg = settings.get("sms", {})
    email_cfg = settings.get("email", {})
    local = _local(clinic, appt.start_at)
    values = _template_values(clinic, lead, appt)
    events: list[FlowEvent] = [
        FlowEvent(key="booked", label="Appointment booked", detail=f"{appt.service} · {values['date']} at {values['time']}", at=iso_z(utcnow()))
    ]
    record(db, "lead_qualification", f"Appointment booked for {lead.name}", detail=events[0].detail, lead_id=lead.id, appointment_id=appt.id)

    # Cancel any pending missed-lead follow-ups — the patient booked.
    for fu in db.scalars(
        select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind.like("missed_lead%"), FollowUp.status == "SCHEDULED")
    ):
        fu.status = "CANCELLED"
    # A new booking after a consult means they scheduled treatment — stop nudges.
    for fu in db.scalars(
        select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind.like("treatment_plan%"), FollowUp.status == "SCHEDULED")
    ):
        fu.status = "CANCELLED"

    if is_active(db, "appointment_confirmation"):
        if appt.patient_email:
            subject = email_cfg.get("confirmation_subject", "Your appointment is confirmed")
            body = (
                f"Hi {values['first_name']},\n\n"
                f"Your {appt.service} appointment at {clinic.name} is confirmed for {values['date']} at {values['time']} "
                f"with {appt.provider_name or 'our team'}.\n\n"
                f"Address: {clinic.address_line}, {clinic.city}, {clinic.state} {clinic.zip_code}\n"
                f"Please complete your intake form before you arrive: {values['intake_url']}\n"
                f"Questions or need to reschedule? {values['booking_url']} or call {clinic.phone}.\n\n"
                f"We look forward to seeing you!\n{clinic.name}"
            )
            ics = appointment_ics(
                clinic_name=clinic.name,
                service=appt.service,
                start_at=appt.start_at,
                end_at=appt.end_at,
                address=f"{clinic.address_line}, {clinic.city}, {clinic.state} {clinic.zip_code}",
                description=f"Intake form: {values['intake_url']}",
                uid=f"appt-{appt.id}@practice",
            )
            result = send_email(appt.patient_email, subject, body, ics=ics)
            db.add(FollowUp(lead_id=lead.id, appointment_id=appt.id, kind="confirmation", channel="email", recipient=appt.patient_email,
                            subject=subject, message=body, due_at=utcnow(), status=result.status, sent_at=utcnow(), error=None if result.status != "FAILED" else result.detail))
            record(db, "appointment_confirmation", "Confirmation email " + ("sent" if result.status == "SENT" else "simulated" if result.status == "SIMULATED" else "failed"),
                   detail=f"To {appt.patient_email}", status=result.status, lead_id=lead.id, appointment_id=appt.id, channel="email")
            events.append(FlowEvent(key="email", label="Confirmation email sent", detail=f"To {appt.patient_email}", status=_flow_status(result), channel="email", at=iso_z(utcnow())))
        if appt.patient_phone:
            body = _fmt(sms_cfg.get("confirmation_template", "Your appointment is confirmed for {date} at {time}."), **values)
            result = send_sms(appt.patient_phone, body)
            db.add(FollowUp(lead_id=lead.id, appointment_id=appt.id, kind="confirmation", channel="sms", recipient=appt.patient_phone,
                            message=body, due_at=utcnow(), status=result.status, sent_at=utcnow(), error=None if result.status != "FAILED" else result.detail))
            record(db, "appointment_confirmation", "Confirmation SMS " + ("sent" if result.status == "SENT" else "simulated" if result.status == "SIMULATED" else "failed"),
                   detail=f"To {appt.patient_phone}", status=result.status, lead_id=lead.id, appointment_id=appt.id, channel="sms", count_run=False)
            events.append(FlowEvent(key="sms", label="Confirmation SMS sent", detail=f"To {appt.patient_phone}", status=_flow_status(result), channel="sms", at=iso_z(utcnow())))

    return events


def schedule_reminders(db: Session, clinic: Clinic, lead: Lead, appt: Appointment) -> list[FlowEvent]:
    sms_cfg = (clinic.settings or {}).get("sms", {})
    email_cfg = (clinic.settings or {}).get("email", {})
    values = _template_values(clinic, lead, appt)
    visit_when = _relative_when(clinic, appt.start_at)
    values = {**values, "when": visit_when}
    events: list[FlowEvent] = []
    chosen = preferred_channel(appt.patient_email, appt.patient_phone)
    if not is_active(db, "sms_reminder") or not chosen:
        return events
    channel, recipient = chosen
    auto = get_automation(db, "sms_reminder")
    hours_list = list((auto.config or {}).get("reminder_hours", [24, 2, 0.5]) if auto else [24, 2, 0.5])
    keep, skipped = _planned_reminders(hours_list, appt.start_at, utcnow())
    template = sms_cfg.get("reminder_template", "Reminder: {first_name}, your {clinic} appointment is {when}.")
    if "{when}" in template and "at {time}" in template:
        template = template.replace(" at {time}", "")
    reminder_subject = email_cfg.get("reminder_subject", "Reminder: your BrightSmile Dental appointment")
    for hours, reason in skipped:
        kind, label = reminder_kind_and_label(hours)
        events.append(FlowEvent(key=kind, label=label, detail=reason, status="pending", channel=channel))
    for hours, due in keep:
        kind, label = reminder_kind_and_label(hours)
        if channel == "email":
            body = (
                f"Hi {values['first_name']},\n\n"
                f"This is a reminder that your {appt.service} appointment at {clinic.name} is {visit_when}"
                f"{' with ' + appt.provider_name if appt.provider_name else ''}.\n\n"
                f"Address: {clinic.address_line}, {clinic.city}, {clinic.state} {clinic.zip_code}\n"
                f"Need to reschedule? {values['booking_url']} or call {clinic.phone}.\n\n"
                f"{clinic.name}"
            )
            subject = reminder_subject
        else:
            body = _fmt(template, **values)
            subject = None
        db.add(FollowUp(
            lead_id=lead.id, appointment_id=appt.id, kind=kind, channel=channel, recipient=recipient,
            subject=subject, message=body, due_at=due, status="SCHEDULED",
        ))
        events.append(FlowEvent(
            key=kind, label=label,
            detail=f"{channel.upper()} {visit_when} · sends {_relative_when(clinic, due)}",
            status="scheduled", channel=channel, at=iso_z(due),
        ))
    scheduled_hours = [h for h, _ in keep]
    record(
        db, "sms_reminder", "Reminders scheduled",
        detail=", ".join(f"{h:g}h before" for h in scheduled_hours) or "none — visit is too soon",
        status="SCHEDULED", lead_id=lead.id, appointment_id=appt.id, channel=channel,
    )
    return events


def _queue_message(
    db: Session,
    clinic: Clinic,
    lead: Lead,
    *,
    kind: str,
    automation_key: str,
    label: str,
    due: datetime,
    body: str,
    subject: str | None,
    appointment_id: int | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> FlowEvent | None:
    chosen = preferred_channel(email or lead.email, phone or lead.phone)
    if not chosen:
        return None
    channel, recipient = chosen
    db.add(
        FollowUp(
            lead_id=lead.id,
            appointment_id=appointment_id,
            kind=kind,
            channel=channel,
            recipient=recipient,
            subject=subject if channel == "email" else None,
            message=body,
            due_at=due,
            status="SCHEDULED",
        )
    )
    record(
        db,
        automation_key,
        f"{label} scheduled",
        detail=f"{channel.upper()} {_relative_when(clinic, due)}",
        status="SCHEDULED",
        lead_id=lead.id,
        appointment_id=appointment_id,
        channel=channel,
    )
    return FlowEvent(
        key=kind,
        label=label,
        detail=f"{channel.upper()} {_relative_when(clinic, due)}",
        status="scheduled",
        channel=channel,
        at=iso_z(due),
    )


# ── Missed-lead follow-up ────────────────────────────────────────────────────
def schedule_missed_lead_followup(db: Session, clinic: Clinic, lead: Lead, reason: str) -> tuple[FollowUp | None, FlowEvent | None]:
    if lead.status in {"BOOKED", "CONVERTED", "LOST"}:
        return None, None
    if not (lead.phone or lead.email):
        return None, None
    if not is_active(db, "missed_lead_followup"):
        return None, None
    existing = db.scalar(
        select(FollowUp).where(
            FollowUp.lead_id == lead.id,
            FollowUp.kind.like("missed_lead%"),
            FollowUp.status == "SCHEDULED",
        )
    )
    if existing:
        return existing, FlowEvent(
            key="followup",
            label="Follow-up already scheduled",
            detail=_relative_when(clinic, existing.due_at),
            status="scheduled",
            at=iso_z(existing.due_at),
        )

    auto = get_automation(db, "missed_lead_followup")
    cfg = auto.config if auto else {}
    raw_cadence = cfg.get("cadence_hours") or [cfg.get("delay_hours", 24)]
    cadence: list[float] = []
    for item in raw_cadence:
        try:
            hours = float(item)
        except (TypeError, ValueError):
            continue
        if hours > 0:
            cadence.append(hours)
    cadence = [h for h in cadence if h >= 12][:2]
    if not cadence:
        cadence = [24.0]
    template = cfg.get("template") or (
        "Hi {first_name}, this is {clinic}. You were interested in {service_lower}. "
        "Would you like help finding a time? Book here: {booking_url}"
    )
    service = lead.service or "a visit"
    values = _template_values(clinic, lead)
    body = _fmt(
        template,
        **values,
        service_lower=service.lower() if service != "Invisalign" else "Invisalign",
    )
    chosen = preferred_channel(lead.email, lead.phone)
    if not chosen:
        return None, None
    channel, recipient = chosen
    first_fu: FollowUp | None = None
    first_event: FlowEvent | None = None
    for index, hours in enumerate(cadence[:3]):
        due = utcnow() + timedelta(hours=hours)
        kind = "missed_lead" if index == 0 else f"missed_lead_{int(hours)}h"
        fu = FollowUp(
            lead_id=lead.id,
            kind=kind,
            channel=channel,
            recipient=recipient,
            subject=f"Still interested in {service}?" if channel == "email" else None,
            message=body,
            due_at=due,
            status="SCHEDULED",
        )
        db.add(fu)
        if first_fu is None:
            first_fu = fu
            first_event = FlowEvent(
                key="followup",
                label="Follow-up scheduled",
                detail=f"{channel.upper()} {_relative_when(clinic, due)} · {len(cadence)}-step cadence",
                status="scheduled",
                channel=channel,
                at=iso_z(due),
            )
    lead.status = "FOLLOW_UP"
    refresh_lead(lead)
    record(
        db,
        "missed_lead_followup",
        f"Follow-up cadence scheduled for {lead.name or 'lead'}",
        detail=f"{channel.upper()} at {', '.join(str(int(h)) if h.is_integer() else str(h) for h in cadence)}h · {reason}",
        status="SCHEDULED",
        lead_id=lead.id,
        channel=channel,
    )
    return first_fu, first_event


def schedule_review_request(db: Session, clinic: Clinic, lead: Lead, appt: Appointment) -> FlowEvent | None:
    if not is_active(db, "review_request"):
        return None
    existing = db.scalar(
        select(FollowUp).where(FollowUp.appointment_id == appt.id, FollowUp.kind == "review_request")
    )
    if existing:
        return None
    auto = get_automation(db, "review_request")
    delay = float((auto.config or {}).get("delay_hours", 3)) if auto else 3.0
    values = _template_values(clinic, lead, appt)
    review = values["review_url"]
    body = (
        f"Hi {values['first_name']}, thank you for visiting {clinic.name} today! "
        f"If you had a great experience, a quick Google review helps other families find us: {review}"
        if review and review != "your Google listing"
        else (
            f"Hi {values['first_name']}, thank you for visiting {clinic.name} today! "
            f"If you had a great experience, searching for us on Google and leaving a review means a lot."
        )
    )
    return _queue_message(
        db,
        clinic,
        lead,
        kind="review_request",
        automation_key="review_request",
        label="Review request",
        due=utcnow() + timedelta(hours=delay),
        body=body,
        subject="How was your visit?",
        appointment_id=appt.id,
        email=appt.patient_email,
        phone=appt.patient_phone,
    )


def schedule_no_show_recovery(db: Session, clinic: Clinic, lead: Lead, appt: Appointment) -> FlowEvent | None:
    if not is_active(db, "no_show_recovery"):
        return None
    auto = get_automation(db, "no_show_recovery")
    delay = float((auto.config or {}).get("delay_hours", 1)) if auto else 1.0
    values = _template_values(clinic, lead, appt)
    body = (
        f"Hi {values['first_name']}, we missed you today for {values['service']} at {clinic.name}. "
        f"Want a new time? Book here: {values['booking_url']} or call {clinic.phone}."
    )
    return _queue_message(
        db,
        clinic,
        lead,
        kind="no_show_recovery",
        automation_key="no_show_recovery",
        label="No-show recovery",
        due=utcnow() + timedelta(hours=max(delay, 0)),
        body=body,
        subject=f"We missed you at {clinic.name}",
        appointment_id=appt.id,
        email=appt.patient_email,
        phone=appt.patient_phone,
    )


def schedule_hygiene_recall(db: Session, clinic: Clinic, lead: Lead, appt: Appointment) -> list[FlowEvent]:
    if not is_active(db, "hygiene_recall"):
        return []
    service = (appt.service or "").lower()
    if "general" not in service and "clean" not in service and "hygiene" not in service:
        return []
    auto = get_automation(db, "hygiene_recall")
    days = float((auto.config or {}).get("delay_days", 180)) if auto else 180.0
    values = _template_values(clinic, lead, appt)
    body = (
        f"Hi {values['first_name']}, it's about time for your next cleaning at {clinic.name}. "
        f"Book here: {values['booking_url']} or call {clinic.phone}."
    )
    event = _queue_message(
        db,
        clinic,
        lead,
        kind="hygiene_recall",
        automation_key="hygiene_recall",
        label="Hygiene recall",
        due=appt.end_at + timedelta(days=max(days, 0)),
        body=body,
        subject=f"Time for your cleaning at {clinic.name}",
        appointment_id=appt.id,
        email=appt.patient_email,
        phone=appt.patient_phone,
    )
    return [event] if event else []


TREATMENT_PLAN_HINTS = ("invisalign", "aligner", "cosmetic", "implant", "veneer", "whitening", "consult")


def is_treatment_plan_service(service: str | None) -> bool:
    text = (service or "").lower()
    return any(hint in text for hint in TREATMENT_PLAN_HINTS)


def queue_benefits_verification(db: Session, clinic: Clinic, lead: Lead | None, fields: dict) -> AutomationEvent | None:
    """Front-desk task after a booked visit with a real insurance plan."""
    if lead is None:
        return None
    plan = (fields or {}).get("insurance") or lead.insurance
    if not plan or plan in {"Self-pay", "Not sure"}:
        return None
    member_id = (fields or {}).get("insurance_member_id")
    if member_id and member_id != "skipped":
        note = f"Member ID: {member_id}"
        if note not in (lead.notes or ""):
            lead.notes = ((lead.notes or "") + "\n" + note).strip()
        detail = f"{plan} · ID {member_id}"
    else:
        detail = f"{plan} · ID not captured — verify at check-in"
    return record(
        db,
        "verify_benefits",
        "Verify insurance benefits",
        detail=detail,
        status="PENDING",
        lead_id=lead.id,
        channel="staff",
        count_run=True,
    )


def schedule_treatment_plan_followup(db: Session, clinic: Clinic, lead: Lead, appt: Appointment | None = None) -> list[FlowEvent]:
    """3-day / 10-day nudge after a high-value consult, or when staff starts it from the lead card."""
    if not is_active(db, "treatment_plan_followup"):
        return []
    service = (appt.service if appt else None) or lead.service
    if appt is not None and not is_treatment_plan_service(service):
        return []
    existing = db.scalar(
        select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind.like("treatment_plan%"), FollowUp.status == "SCHEDULED")
    )
    if existing:
        return []
    auto = get_automation(db, "treatment_plan_followup")
    raw = (auto.config or {}).get("cadence_days") if auto else [3, 10]
    cadence: list[float] = []
    for item in raw or [3, 10]:
        try:
            days = float(item)
        except (TypeError, ValueError):
            continue
        if days > 0:
            cadence.append(days)
    if not cadence:
        cadence = [3.0, 10.0]
    values = _template_values(clinic, lead, appt)
    label = service or values.get("service") or "treatment"
    template = (auto.config or {}).get("template") if auto else None
    values = {**values, "service": label}
    body = _fmt(
        template
        or (
            "Hi {first_name}, this is {clinic}. Ready to schedule your {service} treatment? "
            "Book here: {booking_url} or call {phone} — we can answer remaining questions first."
        ),
        **values,
    )
    events: list[FlowEvent] = []
    for index, days in enumerate(cadence[:4]):
        kind = "treatment_plan" if index == 0 else f"treatment_plan_{int(days)}d"
        event = _queue_message(
            db,
            clinic,
            lead,
            kind=kind,
            automation_key="treatment_plan_followup",
            label="Treatment-plan follow-up" if index == 0 else f"Treatment-plan follow-up ({int(days)}d)",
            due=utcnow() + timedelta(days=days),
            body=body,
            subject=f"Ready to schedule {label}?",
            appointment_id=appt.id if appt else None,
            email=appt.patient_email if appt else lead.email,
            phone=appt.patient_phone if appt else lead.phone,
        )
        if event:
            events.append(event)
    return events


def notify_waitlist(db: Session, appt: Appointment) -> int:
    clinic = db.get(Clinic, appt.clinic_id)
    if clinic is None:
        return 0
    waiting = db.scalars(
        select(WaitlistEntry)
        .where(WaitlistEntry.clinic_id == clinic.id, WaitlistEntry.status == "WAITING")
        .order_by(WaitlistEntry.created_at)
        .limit(8)
    ).all()
    notified = 0
    for entry in waiting:
        if entry.service and appt.service and entry.service.lower() not in {appt.service.lower(), "any"}:
            continue
        chosen = preferred_channel(entry.email, entry.phone)
        if not chosen:
            continue
        channel, recipient = chosen
        body = (
            f"Hi {entry.name.split()[0] if entry.name else 'there'}, a {appt.service} opening just came up at {clinic.name}. "
            f"Book it here: {_booking_url()} or call {clinic.phone}."
        )
        result = send_sms(recipient, body) if channel == "sms" else send_email(
            recipient, f"An opening at {clinic.name}", body
        )
        entry.status = "NOTIFIED"
        record(
            db,
            "sms_reminder",
            f"Waitlist notified: {entry.name}",
            detail=f"{channel.upper()} · {appt.service} opening",
            status=result.status,
            channel=channel,
        )
        notified += 1
        if notified >= 3:
            break
    return notified


def alert_staff_handoff(db: Session, clinic: Clinic, convo: Conversation, lead: Lead | None) -> FlowEvent | None:
    if not is_active(db, "staff_handoff_alert"):
        return None
    who = (lead.name if lead and lead.name else "A website visitor")
    service = (lead.service if lead else None) or convo.service or "an unspecified visit"
    contact = " / ".join(p for p in ((lead.phone if lead else None), (lead.email if lead else None)) if p) or "no contact yet"
    body = (
        f"{who} asked to speak with a person in the AI chat.\n\n"
        f"Service: {service}\nContact: {contact}\n"
        f"Open the conversation: {_site_url()}/admin/conversations\n"
        f"Conversation id: {convo.id}"
    )
    result = send_email(clinic.email, f"Chat handoff at {clinic.name}", body)
    record(
        db,
        "staff_handoff_alert",
        f"Front desk alerted for {who}",
        detail=result.detail,
        status=result.status,
        lead_id=lead.id if lead else None,
        channel="email",
    )
    return FlowEvent(
        key="staff_handoff",
        label="Front desk alerted",
        detail=f"Email to {clinic.email}",
        status=_flow_status(result),
        channel="email",
    )


def phone_digits(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


def handle_inbound_sms(db: Session, clinic: Clinic, from_phone: str, body: str) -> str:
    """Confirm (C) or offer reschedule (R) for the next appointment on this number."""
    digits = phone_digits(from_phone)
    if not digits:
        return f"We couldn't read that number. Please call {clinic.phone}."
    text = (body or "").strip().upper()
    token = text.split()[0] if text else ""
    upcoming = [
        a
        for a in db.scalars(
            select(Appointment)
            .where(Appointment.clinic_id == clinic.id, Appointment.status == "CONFIRMED", Appointment.start_at >= utcnow())
            .order_by(Appointment.start_at)
        ).all()
        if phone_digits(a.patient_phone) == digits
    ]
    if token in {"C", "CONFIRM", "YES", "Y"}:
        if not upcoming:
            return f"We don't have an upcoming visit on file for this number. Book here: {_booking_url()}"
        appt = upcoming[0]
        local = _local(clinic, appt.start_at)
        return f"You're confirmed for {appt.service} on {format_day(local)} at {format_time(local)}. See you then — {clinic.name}."
    if token in {"R", "RESCHEDULE"}:
        return f"No problem — pick a new time here: {_booking_url()} or call {clinic.phone}."
    return (
        f"Thanks for texting {clinic.name}. Reply C to confirm your visit, R to reschedule, "
        f"or book here: {_booking_url()}"
    )


def handle_missed_call(db: Session, clinic: Clinic, from_phone: str) -> DeliveryResult | None:
    if not from_phone:
        return None
    body = (
        f"Sorry we missed your call at {clinic.name}. We're happy to help — "
        f"book online {_booking_url()} or call us back at {clinic.phone}."
    )
    result = send_sms(from_phone, body)
    record(db, "sms_reminder", "Missed-call text sent", detail=result.detail, status=result.status, channel="sms")
    return result


def _cancel_reason(db: Session, fu: FollowUp) -> str | None:
    """Don't send reminders that no longer match the visit, or duplicate channels."""
    kind = fu.kind or ""
    if kind.startswith("missed_lead") and fu.due_at and fu.created_at:
        if (fu.due_at - fu.created_at) < timedelta(hours=12):
            later = db.scalar(
                select(FollowUp).where(
                    FollowUp.lead_id == fu.lead_id,
                    FollowUp.kind.like("missed_lead%"),
                    FollowUp.status == "SCHEDULED",
                    FollowUp.id != fu.id,
                    FollowUp.due_at > fu.due_at,
                )
            )
            if later:
                return "Superseded by a later follow-up"
    if not kind.startswith("reminder"):
        return None
    appt = db.get(Appointment, fu.appointment_id) if fu.appointment_id else None
    if appt is None:
        return "Appointment missing"
    if appt.status != "CONFIRMED":
        return f"Appointment is {appt.status}"
    if appt.start_at <= utcnow():
        return "Visit already started"
    if fu.channel == "sms":
        email_twin = db.scalar(
            select(FollowUp).where(
                FollowUp.appointment_id == fu.appointment_id,
                FollowUp.kind == fu.kind,
                FollowUp.channel == "email",
                FollowUp.status.in_(("SCHEDULED", "SENT", "SIMULATED")),
                FollowUp.id != fu.id,
            )
        )
        if email_twin:
            return "Email reminder already covers this"
    already = db.scalar(
        select(FollowUp).where(
            FollowUp.appointment_id == fu.appointment_id,
            FollowUp.kind == fu.kind,
            FollowUp.channel == fu.channel,
            FollowUp.status.in_(("SENT", "SIMULATED")),
            FollowUp.id != fu.id,
        )
    )
    if already:
        return "Already sent"
    return None


def deliver_followup(db: Session, fu: FollowUp) -> FollowUp:
    if fu.status != "SCHEDULED":
        return fu
    reason = _cancel_reason(db, fu)
    if reason:
        fu.status = "CANCELLED"
        fu.error = reason
        return fu
    if fu.channel == "sms":
        result = send_sms(fu.recipient, fu.message)
    else:
        result = send_email(fu.recipient, fu.subject or "A message from BrightSmile Dental", fu.message)
    fu.status = result.status
    fu.sent_at = utcnow()
    fu.error = result.detail if result.status == "FAILED" else None
    if fu.kind.startswith("missed_lead"):
        key = "missed_lead_followup"
    elif fu.kind.startswith("treatment_plan"):
        key = "treatment_plan_followup"
    else:
        key = {
            "review_request": "review_request",
            "no_show_recovery": "no_show_recovery",
            "hygiene_recall": "hygiene_recall",
        }.get(fu.kind, "sms_reminder")
    label_map = {
        "missed_lead": "Follow-up",
        "review_request": "Review request",
        "no_show_recovery": "No-show recovery",
        "hygiene_recall": "Hygiene recall",
        "treatment_plan": "Treatment-plan follow-up",
        "reminder_24h": "24-hour reminder",
        "reminder_2h": "2-hour reminder",
        "reminder_30m": "30-minute reminder",
    }
    label = label_map.get(fu.kind, fu.kind.replace("_", " ").replace("reminder ", "") or fu.kind)
    record(db, key, f"{label} {'sent' if result.status == 'SENT' else result.status.lower()}", detail=f"{fu.channel.upper()} to {fu.recipient}",
           status=result.status, lead_id=fu.lead_id, appointment_id=fu.appointment_id, channel=fu.channel)
    lead = db.get(Lead, fu.lead_id)
    if lead:
        lead.last_activity_at = utcnow()
        if fu.kind.startswith("missed_lead") and lead.status == "FOLLOW_UP":
            lead.status = "CONTACTED"
    return fu


def process_due_followups(db: Session) -> int:
    pending = db.scalars(
        select(FollowUp).where(
            FollowUp.status == "SCHEDULED",
            or_(FollowUp.kind.like("reminder%"), FollowUp.kind.like("missed_lead%")),
        ).limit(400)
    ).all()
    for fu in pending:
        reason = _cancel_reason(db, fu)
        if reason:
            fu.status = "CANCELLED"
            fu.error = reason
    due = db.scalars(select(FollowUp).where(FollowUp.status == "SCHEDULED", FollowUp.due_at <= utcnow()).limit(100)).all()
    sent = 0
    for fu in due:
        before = fu.status
        deliver_followup(db, fu)
        if fu.status != before and fu.status != "CANCELLED":
            sent += 1
    return sent


def sweep_abandoned_conversations(db: Session) -> int:
    """Mark idle chats as abandoned and trigger missed-lead follow-ups."""
    cutoff = utcnow() - timedelta(minutes=get_settings().abandoned_chat_minutes)
    convos = db.scalars(select(Conversation).where(Conversation.status == "ACTIVE", Conversation.last_message_at < cutoff).limit(200)).all()
    clinic = db.scalar(select(Clinic))
    for convo in convos:
        end_conversation(db, clinic, convo, reason="Chat inactive")
    return len(convos)


def end_conversation(db: Session, clinic: Clinic | None, convo: Conversation, reason: str) -> FlowEvent | None:
    if convo.status != "ACTIVE":
        return None
    convo.ended_at = utcnow()
    event = None
    lead = db.get(Lead, convo.lead_id) if convo.lead_id else None
    if lead and clinic and lead.service and lead.status not in {"BOOKED", "CONVERTED"}:
        _, event = schedule_missed_lead_followup(db, clinic, lead, reason)
    convo.status = "FOLLOW_UP" if event else "ABANDONED" if lead and lead.service else "CLOSED"
    return event

"""Seeds the database with the fictional clinic and a coherent set of DEMO data.

Dataset (last 30 days): 247 AI conversations, 106 leads (86 qualified),
42 appointments, 31 missed-lead follow-ups. Every record is flagged is_demo.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from .knowledge_base import AUTOMATIONS_SEED, CLINIC_SEED, FAQS_SEED, INTEGRATIONS_SEED, SERVICES_SEED, STAFF_USERS_SEED
from .models import (
    FAQ, Appointment, AuditLog, Automation, AutomationEvent, Clinic, Conversation, FollowUp, IntakeSubmission, Integration, KnowledgeDocument, Lead, Message,
    Service, User, WaitlistEntry, utcnow,
)
from .services.leads import score_lead
from .services.scheduler import DAY_KEYS, duration_for, format_day, format_time, provider_for, to_utc_naive

logger = logging.getLogger("brightsmile")

FIRST = ["Olivia", "Liam", "Emma", "Noah", "Ava", "Ethan", "Sophia", "Mason", "Isabella", "Lucas", "Mia", "James", "Charlotte",
         "Benjamin", "Amelia", "Elijah", "Harper", "Daniel", "Evelyn", "Henry", "Abigail", "Jackson", "Emily", "Sebastian",
         "Ella", "Aiden", "Grace", "Carter", "Chloe", "Wyatt", "Zoe", "Owen", "Nora", "Caleb", "Lily", "Isaac", "Hannah",
         "Gabriel", "Layla", "Julian", "Riley", "Mateo", "Aria", "Luke", "Camila", "Levi", "Maya", "Nathan", "Sofia", "Ryan"]
LAST = ["Johnson", "Martinez", "Nguyen", "Patel", "Thompson", "Garcia", "Robinson", "Walker", "Hernandez", "Lopez", "Young",
        "Allen", "King", "Wright", "Scott", "Torres", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera",
        "Campbell", "Mitchell", "Carter", "Roberts", "Phillips", "Evans", "Turner", "Parker", "Collins", "Edwards", "Stewart",
        "Morris", "Murphy", "Cook", "Rogers", "Morgan", "Cooper", "Peterson", "Reed", "Bailey", "Bell", "Gomez", "Kelly", "Howard"]
SERVICE_WEIGHTS = [("General Dentistry", 30), ("Invisalign", 22), ("Dental Implants", 18), ("Cosmetic Dentistry", 15), ("Emergency Dentistry", 15)]
INSURERS = ["Delta Dental", "Cigna", "Aetna", "MetLife", "Blue Cross Blue Shield", "Guardian", "Humana", "Self-pay", "Not sure"]
INTEREST_LINES = {
    "General Dentistry": "I need a cleaning and checkup — it's been a while.",
    "Invisalign": "I'm interested in Invisalign. Do you offer it?",
    "Dental Implants": "I'm missing a tooth and I'm interested in implants.",
    "Cosmetic Dentistry": "Do you do teeth whitening?",
    "Emergency Dentistry": "I cracked my tooth and it really hurts. Can I be seen today?",
}
TOPIC = {
    "General Dentistry": "Checkup & Cleaning Appointment",
    "Invisalign": "Invisalign Consultation",
    "Dental Implants": "Dental Implant Consultation",
    "Cosmetic Dentistry": "Cosmetic Consultation",
    "Emergency Dentistry": "Emergency Appointment",
}
INFO_TOPICS = [
    ("Insurance Question", "Do you accept Delta Dental?", "Yes — we're in-network with Delta Dental. We'll verify your benefits before your visit and file the claim for you."),
    ("Insurance Question", "Do you take Cigna insurance?", "Yes — we're in-network with Cigna. We'll verify your benefits before your visit and file the claim for you."),
    ("Hours & Location", "What are your hours on Saturday?", "On Saturdays we're open 9 AM–1 PM. Weekdays we open at 8 AM."),
    ("Hours & Location", "Where are you located?", "We're at 4120 Bright Leaf Blvd, Suite 200, Austin, TX 78731, with free covered parking."),
    ("Pricing Question", "How much does a cleaning cost without insurance?", "Without insurance, our new patient special — exam, cleaning and X-rays — is $129."),
    ("Pricing Question", "Do you offer payment plans?", "Yes. We offer flexible monthly payments through CareCredit and Sunbit, plus our $29/month BrightSmile Membership."),
    ("Services Question", "Do you see kids?", "Yes — we're a family practice and see children from age 3."),
    ("Services Question", "Do you offer sedation? I'm nervous about dentists.", "We offer comfort options including nitrous oxide for eligible patients. Your dentist will discuss what's appropriate."),
    ("General Question", "Do you speak Spanish?", "Our team speaks English and Spanish. Hablamos español."),
    ("Services Question", "How long does Invisalign take?", "Treatment time varies by case; your free consultation includes a 3D scan and an estimated timeline."),
]


def _now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def seed_reference_data(db: Session) -> Clinic:
    clinic = Clinic(**CLINIC_SEED)
    db.add(clinic)
    db.flush()
    for s in SERVICES_SEED:
        db.add(Service(clinic_id=clinic.id, **s))
    for i, f in enumerate(FAQS_SEED):
        db.add(FAQ(clinic_id=clinic.id, sort_order=i, **f))
    for u in STAFF_USERS_SEED:
        db.add(User(clinic_id=clinic.id, **u))
    for a in AUTOMATIONS_SEED:
        db.add(Automation(clinic_id=clinic.id, status="ACTIVE", **a))
    for p in INTEGRATIONS_SEED:
        db.add(Integration(clinic_id=clinic.id, provider=p, enabled=True, config={}))
    db.flush()
    db.refresh(clinic)
    return clinic


def _msg(convo: Conversation, role: str, content: str, at: datetime, **meta) -> Message:
    return Message(conversation_id=convo.id, role=role, content=content, created_at=at, meta=meta)


def _business_slot(rng: random.Random, clinic: Clinic, day, tz: ZoneInfo, service: str) -> datetime | None:
    hours = clinic.hours.get(DAY_KEYS[day.weekday()])
    if not hours:
        return None
    open_h = int(hours["open"][:2])
    close_h = int(hours["close"][:2])
    choices = [h * 60 + m for h in range(open_h, close_h) for m in (0, 30) if not (12 <= h < 13 and day.weekday() < 5)]
    choices = [c for c in choices if c + duration_for(service) <= close_h * 60]
    if not choices:
        return None
    minute = rng.choice(choices)
    return datetime.combine(day, time(minute // 60, minute % 60), tz)


def seed_demo_activity(db: Session, clinic: Clinic, seed: int = 42) -> None:
    rng = random.Random(seed)
    tz = ZoneInfo(clinic.timezone)
    now = utcnow()
    now_local = _now_local(tz)
    services = [s for s, _ in SERVICE_WEIGHTS]
    weights = [w for _, w in SERVICE_WEIGHTS]
    used_names: set[str] = set()

    def person() -> tuple[str, str, str]:
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used_names:
                used_names.add(name)
                break
        first, last = name.split()
        phone = f"(512) 555-01{rng.randint(0, 99):02d}"
        email = f"{first.lower()}.{last.lower()}@example.com"
        return name, phone, email

    def created_time() -> datetime:
        # Skew toward recent days (growing adoption); keep inside business-ish hours.
        days_ago = int(rng.triangular(0, 28, 14))
        base = now - timedelta(days=days_ago)
        return base.replace(hour=rng.randint(13, 23), minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0) if days_ago else now - timedelta(minutes=rng.randint(20, 600))

    events: list[AutomationEvent] = []

    def add_event(key: str, title: str, at: datetime, lead: Lead | None, status: str = "COMPLETED", detail: str = "", channel: str | None = None, appt: Appointment | None = None):
        events.append(AutomationEvent(automation_key=key, title=title, detail=detail, status=status, channel=channel,
                                      lead_id=lead.id if lead else None, appointment_id=appt.id if appt else None, created_at=at))

    def make_convo(at: datetime, lead: Lead | None, topic: str, service: str | None, status: str, stage: str, score: str) -> Conversation:
        c = Conversation(clinic_id=clinic.id, lead_id=lead.id if lead else None, intent=topic, service=service, status=status, stage=stage,
                         lead_score=score, is_demo=True, created_at=at, updated_at=at, last_message_at=at, page=rng.choice(["/", "/services/invisalign", "/services/dental-implants", "/", "/book", "/services"]),
                         state={}, ended_at=at + timedelta(minutes=6))
        db.add(c)
        db.flush()
        return c

    def lead_row(name, phone, email, service, status, source, at, **kw) -> Lead:
        lead = Lead(clinic_id=clinic.id, name=name, phone=phone, email=email, service=service, status=status, source=source,
                    is_demo=True, created_at=at, updated_at=at, last_activity_at=at, **kw)
        lead.score, lead.score_label = score_lead(lead)
        db.add(lead)
        db.flush()
        return lead

    def booking_messages(c: Conversation, lead: Lead, at: datetime, slot_label: str):
        t = at
        step = lambda: t + timedelta(seconds=rng.randint(15, 70))  # noqa: E731
        msgs = [
            ("user", INTEREST_LINES[lead.service]),
            ("assistant", f"Absolutely — I can help you with that. Is the appointment for yourself, and have you visited BrightSmile before?"),
            ("user", "I'm a new patient" if lead.patient_type == "new" else "I'm an existing patient"),
            ("assistant", "Perfect. What's the best name, mobile number and email for the appointment?"),
            ("user", f"My details: {lead.name} · {lead.phone} · {lead.email}"),
            ("assistant", "Do you have dental insurance? If so, which provider?"),
            ("user", lead.insurance or "Not sure"),
            ("assistant", f"Here are the next available appointments with {provider_for(lead.service)}:"),
            ("user", f"I'll take {slot_label}"),
            ("assistant", f"You're all set, {lead.name.split()[0]}! ✅ Your appointment is confirmed for {slot_label}. You'll get a confirmation by email and text, plus reminders before your visit."),
        ]
        for role, content in msgs:
            t = step()
            db.add(_msg(c, role, content, t))
        c.last_message_at = t

    # ── 1) Booked / converted leads (42 appointments) ────────────────────────
    appt_plan: list[tuple[str, str]] = [("ai_chat", "chat")] * 26 + [("booking_form", "form")] * 9 + [("phone", "phone")] * 7
    rng.shuffle(appt_plan)
    # 14 in the past (completed), the rest upcoming; a few today when the clinic is open.
    today_open = clinic.hours.get(DAY_KEYS[now_local.weekday()]) is not None
    for i, (source, _) in enumerate(appt_plan):
        service = rng.choices(services, weights)[0]
        name, phone, email = person()
        if i < 14:
            day = (now_local - timedelta(days=rng.randint(1, 20))).date()
        elif i < 18 and today_open:
            day = now_local.date()
        else:
            day = (now_local + timedelta(days=rng.randint(1, 14))).date()
        start_local = None
        for _ in range(10):
            start_local = _business_slot(rng, clinic, day, tz, service)
            if start_local:
                break
            day += timedelta(days=1)
        assert start_local is not None
        start = to_utc_naive(start_local)
        completed = start < now
        # Bookings are created 1-20 days before "now" (not before the appointment),
        # so the daily series looks like a steady practice rather than a spike today.
        booked_days_ago = rng.randint(1, 20)
        created = now - timedelta(days=booked_days_ago, hours=rng.randint(0, 10), minutes=rng.randint(0, 59))
        created = min(created, start - timedelta(hours=2))
        lead = lead_row(name, phone, email, service, "CONVERTED" if completed else "BOOKED", source, created,
                        patient_type=rng.choice(["new", "new", "new", "existing"]), insurance=rng.choice(INSURERS),
                        urgency="emergency" if service == "Emergency Dentistry" else rng.choice(["soon", "normal", "flexible"]),
                        preferred_date=rng.choice(["Earliest available", "This week", "Next week"]), preferred_time=rng.choice(["Morning", "Afternoon", "Any time"]))
        appt = Appointment(clinic_id=clinic.id, lead_id=lead.id, patient_name=name, patient_phone=phone, patient_email=email, service=service,
                           provider_name=provider_for(service), start_at=start, end_at=start + timedelta(minutes=duration_for(service)),
                           status="COMPLETED" if completed else "CONFIRMED", source=source, scheduler="mock", is_demo=True,
                           notes=f"Insurance: {lead.insurance} · Patient: {lead.patient_type}", created_at=created, updated_at=created)
        db.add(appt)
        db.flush()
        slot_label = f"{format_day(start_local)} · {format_time(start_local)}"
        if source == "ai_chat":
            c = make_convo(created - timedelta(minutes=5), lead, TOPIC[service], service, "BOOKED", "booked", "HOT")
            appt.conversation_id = c.id
            booking_messages(c, lead, created - timedelta(minutes=5), slot_label)
            add_event("lead_qualification", f"Appointment booked for {name}", created, lead, detail=f"{service} · {slot_label}", appt=appt)
        add_event("appointment_confirmation", "Confirmation email simulated", created, lead, "SIMULATED", f"To {email}", "email", appt)
        db.add(FollowUp(lead_id=lead.id, appointment_id=appt.id, kind="confirmation", channel="email", recipient=email, subject="Your BrightSmile Dental appointment is confirmed",
                        message=f"Your {service} appointment is confirmed for {slot_label}.", due_at=created, status="SIMULATED", sent_at=created, created_at=created))
        db.add(FollowUp(lead_id=lead.id, appointment_id=appt.id, kind="confirmation", channel="sms", recipient=phone,
                        message=f"Hi {name.split()[0]}! Your {service} visit at BrightSmile Dental is confirmed for {slot_label}.", due_at=created, status="SIMULATED", sent_at=created, created_at=created))
        for hours in (24, 2):
            due = start - timedelta(hours=hours)
            sent = due < now
            db.add(FollowUp(lead_id=lead.id, appointment_id=appt.id, kind=f"reminder_{hours}h", channel="sms", recipient=phone,
                            message=f"Reminder: {name.split()[0]}, your BrightSmile Dental appointment is {'tomorrow' if hours == 24 else 'in 2 hours'} at {format_time(start_local)}.",
                            due_at=due, status="SIMULATED" if sent else "SCHEDULED", sent_at=due if sent else None, created_at=created))
            if sent:
                add_event("sms_reminder", f"{hours}-hour reminder simulated", due, lead, "SIMULATED", f"SMS to {phone}", "sms", appt)
        if completed:
            due = appt.end_at + timedelta(hours=3)
            sent = due < now
            db.add(FollowUp(lead_id=lead.id, appointment_id=appt.id, kind="review_request", channel="sms", recipient=phone,
                            message=f"Hi {name.split()[0]}, thank you for visiting BrightSmile Dental today! Would you mind leaving us a quick review?",
                            due_at=due, status="SIMULATED" if sent else "SCHEDULED", sent_at=due if sent else None, created_at=created))
            if sent:
                add_event("review_request", "Review request simulated", due, lead, "SIMULATED", f"SMS to {phone}", "sms", appt)

    # ── 2) Missed-lead follow-ups (31) ───────────────────────────────────────
    fu_statuses = ["FOLLOW_UP"] * 15 + ["CONTACTED"] * 11 + ["LOST"] * 5
    rng.shuffle(fu_statuses)
    for status in fu_statuses:
        service = rng.choices(services[:4], weights[:4])[0]
        name, phone, email = person()
        # Pending follow-ups are recent so their 24h reminder is still in the future.
        created = now - timedelta(hours=rng.randint(2, 20)) if status == "FOLLOW_UP" else created_time() - timedelta(days=2)
        lead = lead_row(name, phone if rng.random() > 0.15 else None, email, service, status, "ai_chat", created,
                        patient_type="new", insurance=rng.choice(INSURERS))
        c = make_convo(created, lead, TOPIC[service], service, "FOLLOW_UP", "followup_scheduled", lead.score_label)
        t = created
        for role, content in [
            ("user", INTEREST_LINES[service]),
            ("assistant", f"Yes — we offer {service.lower() if service != 'Invisalign' else 'Invisalign'} at BrightSmile Dental. Would you like to schedule a consultation?"),
            ("user", "Not right now, maybe later."),
            ("assistant", f"No problem! If you'd like, leave your name and a phone number or email and our team will send you more information — no pressure."),
            ("user", f"My details: {name} · {lead.phone or ''} · {email}".replace(" ·  ·", " ·")),
            ("assistant", f"No problem, {name.split()[0]}! I've noted your interest. Our team will follow up tomorrow in case you'd like help finding a time."),
        ]:
            t += timedelta(seconds=rng.randint(20, 90))
            db.add(_msg(c, role, content, t))
        c.last_message_at = t
        due = created + timedelta(hours=24)
        sent = status in {"CONTACTED", "LOST"}
        channel = "sms" if lead.phone else "email"
        body = (f"Hi {name.split()[0]}, this is BrightSmile Dental. You were interested in {service.lower() if service != 'Invisalign' else 'Invisalign'}. "
                "Would you like help finding a consultation time? Reply YES or book online: http://localhost:3000/book")
        db.add(FollowUp(lead_id=lead.id, kind="missed_lead", channel=channel, recipient=lead.phone or email, message=body, due_at=due,
                        status="SIMULATED" if sent else "SCHEDULED", sent_at=due if sent else None, created_at=created,
                        subject=None if channel == "sms" else f"Still interested in {service}?"))
        add_event("missed_lead_followup", f"Follow-up scheduled for {name}", created, lead, "SCHEDULED", f"{channel.upper()} in 24 hours", channel)
        if sent:
            add_event("missed_lead_followup", "Follow-up simulated", due, lead, "SIMULATED", f"{channel.upper()} to {lead.phone or email}", channel)
        if status == "LOST":
            lead.notes = "No response after follow-up."

    # ── 3) Qualified (13) and partial NEW leads (20) ─────────────────────────
    for _ in range(13):
        service = rng.choices(services, weights)[0]
        name, phone, email = person()
        created = created_time() if rng.random() < 0.8 else now - timedelta(minutes=rng.randint(40, 60 * 30))
        lead = lead_row(name, phone, email, service, "QUALIFIED", "ai_chat", created, patient_type=rng.choice(["new", "existing"]),
                        insurance=rng.choice(INSURERS), urgency=rng.choice(["soon", "normal", "flexible"]))
        c = make_convo(created, lead, TOPIC[service], service, "ABANDONED", "ask_preference", lead.score_label)
        t = created
        for role, content in [
            ("user", INTEREST_LINES[service]),
            ("assistant", "Absolutely — I can help you get started. Have you visited BrightSmile before?"),
            ("user", "I'm a new patient" if lead.patient_type == "new" else "I'm an existing patient"),
            ("assistant", "Perfect. What's the best name, mobile number and email for the appointment?"),
            ("user", f"My details: {name} · {phone} · {email}"),
            ("assistant", "Do you have a preferred day or time of day?"),
        ]:
            t += timedelta(seconds=rng.randint(20, 90))
            db.add(_msg(c, role, content, t))
        c.last_message_at = t
    for _ in range(20):
        service = rng.choices(services, weights)[0]
        created = created_time()
        lead = Lead(clinic_id=clinic.id, service=service, status="NEW", source="ai_chat", is_demo=True, created_at=created,
                    updated_at=created, last_activity_at=created, patient_type=rng.choice([None, "new"]))
        lead.score, lead.score_label = score_lead(lead)
        db.add(lead)
        db.flush()
        c = make_convo(created, lead, TOPIC[service], service, "ABANDONED", "ask_contact", lead.score_label)
        t = created
        for role, content in [
            ("user", INTEREST_LINES[service]),
            ("assistant", "Absolutely — I can help you get started. Have you visited BrightSmile before?"),
            ("user", "I'm a new patient"),
            ("assistant", "Perfect. What's the best name, mobile number and email for the appointment?"),
        ]:
            t += timedelta(seconds=rng.randint(20, 90))
            db.add(_msg(c, role, content, t))
        c.last_message_at = t

    # ── 4) Anonymous information conversations (157) ────────────────────────
    for _ in range(157):
        created = created_time()
        topic, q, a = rng.choice(INFO_TOPICS)
        c = make_convo(created, None, topic, None, "CLOSED", "start", "COLD")
        t = created
        db.add(_msg(c, "user", q, t))
        db.add(_msg(c, "assistant", a, t + timedelta(seconds=2), retrieved=[topic]))
        if rng.random() < 0.4:
            t2 = t + timedelta(seconds=rng.randint(20, 60))
            db.add(_msg(c, "user", "Thanks!", t2))
            db.add(_msg(c, "assistant", "You're welcome! Is there anything else I can help you with?", t2 + timedelta(seconds=2)))
            t = t2
        c.last_message_at = t

    # AI receptionist events for a subset of conversations (activity feed flavor)
    for c in db.scalars(select(Conversation).order_by(Conversation.created_at.desc()).limit(25)):
        add_event("ai_receptionist", f"Answered: {c.intent}", c.created_at, None, detail="Grounded in clinic knowledge base")

    db.add_all(events)
    db.flush()
    # Automation run counters derived from the event log
    for auto in db.scalars(select(Automation)):
        cnt, last = db.execute(select(func.count(AutomationEvent.id), func.max(AutomationEvent.created_at)).where(AutomationEvent.automation_key == auto.key)).one()
        auto.runs_count = cnt if auto.key != "ai_receptionist" else 247
        auto.last_run_at = last
    db.add(AuditLog(actor="system", action="demo.seeded", detail={"seed": seed}))


SEED_EMAIL_DOMAIN = "@example.com"


def _is_seed_email(email: str | None) -> bool:
    return bool(email) and email.lower().endswith(SEED_EMAIL_DOMAIN)


def promote_live_records(db: Session) -> int:
    """Treat real patient activity as live even if it was created while DEMO_MODE was on.

    Seeded rows use reserved ``@example.com`` addresses. Live rows are never demoted.
    """
    changed = 0
    live_lead_ids: set[int] = set()
    for lead in db.scalars(select(Lead)).all():
        if lead.is_demo and lead.email and not _is_seed_email(lead.email):
            lead.is_demo = False
            changed += 1
        if not lead.is_demo:
            live_lead_ids.add(lead.id)
    for appt in db.scalars(select(Appointment)).all():
        live = (bool(appt.patient_email) and not _is_seed_email(appt.patient_email)) or (appt.lead_id in live_lead_ids)
        if appt.is_demo and live:
            appt.is_demo = False
            changed += 1
            if appt.lead_id:
                lead = db.get(Lead, appt.lead_id)
                if lead and lead.is_demo:
                    lead.is_demo = False
                    live_lead_ids.add(lead.id)
                    changed += 1
        if not appt.is_demo and appt.lead_id:
            live_lead_ids.add(appt.lead_id)
    for convo in db.scalars(select(Conversation)).all():
        live = convo.lead_id in live_lead_ids or (convo.lead_id is None and bool(convo.state))
        if convo.is_demo and live:
            convo.is_demo = False
            changed += 1
    return changed


def purge_demo_activity(db: Session) -> int:
    """Remove seeded fake patients, chats, appointments and their automation log.

    Only ``@example.com`` rows are deleted, so a missed flush cannot wipe live bookings.
    """
    db.flush()
    seed_leads = [
        lead_id for lead_id, email in db.execute(select(Lead.id, Lead.email)) if _is_seed_email(email)
    ]
    seed_appts = [
        appt_id for appt_id, email in db.execute(select(Appointment.id, Appointment.patient_email)) if _is_seed_email(email)
    ]
    seed_lead_set = set(seed_leads)
    seed_convos: list[str] = []
    for cid, lead_id, state in db.execute(select(Conversation.id, Conversation.lead_id, Conversation.state)):
        if lead_id in seed_lead_set or (lead_id is None and not state):
            seed_convos.append(cid)
    if not (seed_leads or seed_appts or seed_convos):
        return 0

    event_clauses = []
    if seed_leads:
        event_clauses.append(AutomationEvent.lead_id.in_(seed_leads))
    if seed_appts:
        event_clauses.append(AutomationEvent.appointment_id.in_(seed_appts))
    if event_clauses:
        db.execute(delete(AutomationEvent).where(or_(*event_clauses)))
    db.execute(delete(AutomationEvent).where(and_(AutomationEvent.lead_id.is_(None), AutomationEvent.appointment_id.is_(None))))
    if seed_leads:
        db.execute(delete(FollowUp).where(FollowUp.lead_id.in_(seed_leads)))
    if seed_appts:
        db.execute(delete(FollowUp).where(FollowUp.appointment_id.in_(seed_appts)))
    if seed_convos:
        db.execute(delete(Message).where(Message.conversation_id.in_(seed_convos)))
        db.execute(delete(Conversation).where(Conversation.id.in_(seed_convos)))
    if seed_appts:
        db.execute(delete(Appointment).where(Appointment.id.in_(seed_appts)))
    if seed_leads:
        db.execute(delete(Lead).where(Lead.id.in_(seed_leads)))

    for auto in db.scalars(select(Automation)):
        cnt, last = db.execute(
            select(func.count(AutomationEvent.id), func.max(AutomationEvent.created_at)).where(AutomationEvent.automation_key == auto.key)
        ).one()
        auto.runs_count = int(cnt or 0)
        auto.last_run_at = last
    return len(seed_leads) + len(seed_appts) + len(seed_convos)


def ensure_seeded(db: Session) -> bool:
    created = False
    if not db.scalar(select(func.count(Clinic.id))):
        seed_reference_data(db)
        db.commit()
        created = True
    clinic = db.scalar(select(Clinic))
    if clinic:
        existing = {a.key for a in db.scalars(select(Automation))}
        for spec in AUTOMATIONS_SEED:
            if spec["key"] not in existing:
                db.add(Automation(clinic_id=clinic.id, status="ACTIVE", **spec))
        settings = dict(clinic.settings or {})
        brand = {**(CLINIC_SEED.get("settings", {}).get("brand") or {}), **(settings.get("brand") or {})}
        if settings.get("brand") != brand:
            settings["brand"] = brand
            clinic.settings = settings
        db.flush()
    promoted = promote_live_records(db)
    db.flush()
    purged = purge_demo_activity(db)
    if promoted or purged:
        logger.info("Live dashboard: promoted %s records, removed %s seeded rows", promoted, purged)
        db.commit()
    elif clinic:
        db.commit()
    return created


def reset_demo(db: Session) -> None:
    for model in (AutomationEvent, FollowUp, Message, Appointment, Conversation, Lead, AuditLog, KnowledgeDocument, IntakeSubmission, WaitlistEntry, Integration, Automation, User, FAQ, Service, Clinic):
        db.execute(delete(model))
    db.commit()
    seed_reference_data(db)
    db.commit()



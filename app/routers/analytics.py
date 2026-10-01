"""Dashboard analytics. Counts come from live (non-seeded) records.

Website visitors are only a synthetic series when explicitly including demo
data; live mode does not invent traffic numbers.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..config import get_settings
from ..database import get_db
from ..models import Appointment, AutomationEvent, Conversation, FollowUp, Lead
from ..schemas import AppointmentOut, AutomationEventOut
from ..security import require_admin
from ..services.leads import is_qualified

router = APIRouter(prefix="/api/analytics", tags=["Analytics"], dependencies=[Depends(require_admin)])

DAYS = 30
VISITOR_TOTAL = 1284


def _synthetic_visitors(days: list[date]) -> list[int]:
    raw = []
    for i, d in enumerate(days):
        h = int(hashlib.md5(d.isoformat().encode()).hexdigest()[:4], 16) % 13
        weekday_factor = 0.72 if d.weekday() == 6 else 0.85 if d.weekday() == 5 else 1.0
        raw.append((30 + i * 0.9 + h) * weekday_factor)
    scale = VISITOR_TOTAL / sum(raw)
    values = [round(v * scale) for v in raw]
    values[-1] += VISITOR_TOTAL - sum(values)
    return values


@router.get("", summary="Dashboard KPIs, time series, funnel and breakdowns")
def analytics(
    db: Session = Depends(get_db),
    include_demo: bool = Query(default=False, description="Include seeded demonstration records"),
) -> dict:
    clinic = load_clinic(db)
    tz = ZoneInfo(clinic.timezone)
    today = datetime.now(tz).date()
    days = [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    window_start = datetime.combine(days[0], datetime.min.time(), tz).astimezone(timezone.utc).replace(tzinfo=None)

    def local_day(dt: datetime) -> date:
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).date()

    convo_q = select(Conversation)
    lead_q = select(Lead)
    appt_q = select(Appointment).where(Appointment.status != "CANCELLED")
    fu_q = select(FollowUp).where(FollowUp.kind == "missed_lead")
    if not include_demo:
        convo_q = convo_q.where(Conversation.is_demo.is_(False))
        lead_q = lead_q.where(Lead.is_demo.is_(False))
        appt_q = appt_q.where(Appointment.is_demo.is_(False))
        fu_q = fu_q.join(Lead, FollowUp.lead_id == Lead.id).where(Lead.is_demo.is_(False))

    convos = db.scalars(convo_q).all()
    leads = db.scalars(lead_q).all()
    appts = db.scalars(appt_q).all()
    followups = db.scalars(fu_q).all()

    qualified = [l for l in leads if is_qualified(l)]
    visitors = _synthetic_visitors(days) if include_demo else [0] * len(days)
    convo_by_day = Counter(local_day(c.created_at) for c in convos if c.created_at >= window_start)
    lead_by_day = Counter(local_day(l.created_at) for l in qualified if l.created_at and l.created_at >= window_start)
    appt_by_day = Counter(local_day(a.created_at) for a in appts if a.created_at and a.created_at >= window_start)

    series = [
        {
            "date": d.isoformat(),
            "label": d.strftime("%b ") + str(d.day),
            "visitors": visitors[i],
            "conversations": convo_by_day.get(d, 0),
            "leads": lead_by_day.get(d, 0),
            "appointments": appt_by_day.get(d, 0),
        }
        for i, d in enumerate(days)
    ]

    total_conversations = len(convos)
    total_leads_with_contact = len([l for l in leads if l.name or l.phone or l.email])
    n_appts = len(appts)
    visitor_total = sum(visitors)
    kpis = {
        "visitors": visitor_total,
        "conversations": total_conversations,
        "qualified_leads": len(qualified),
        "appointments": n_appts,
        "followups": len(followups),
        "conversion_rate": round(100 * n_appts / len(qualified), 1) if qualified else 0.0,
        "chat_rate": round(100 * total_conversations / max(visitor_total, 1), 1) if include_demo else 0.0,
        "ai_booked": len([a for a in appts if a.source == "ai_chat"]),
    }
    half = DAYS // 2

    def delta(key: str) -> float:
        a = sum(s[key] for s in series[:half])
        b = sum(s[key] for s in series[half:])
        return round(100 * (b - a) / a, 1) if a else 0.0

    trends = {k: delta(k) for k in ("visitors", "conversations", "leads", "appointments")}

    funnel: list[dict] = []
    if include_demo:
        funnel.append({"stage": "Website visitors", "value": kpis["visitors"]})
    funnel.extend([
        {"stage": "AI conversations", "value": total_conversations},
        {"stage": "Leads captured", "value": total_leads_with_contact},
        {"stage": "Qualified leads", "value": len(qualified)},
        {"stage": "Appointments", "value": n_appts},
    ])
    by_service = Counter(l.service or "Unspecified" for l in leads)
    sources = Counter(a.source for a in appts)
    source_labels = {"ai_chat": "AI Receptionist", "booking_form": "Online booking form", "phone": "Phone", "manual": "Front desk", "walk_in": "Walk-in"}
    status_counts = Counter(l.status for l in leads)
    intents = Counter(c.intent for c in convos)
    hours = Counter(c.created_at.replace(tzinfo=timezone.utc).astimezone(tz).hour for c in convos)
    after_hours = sum(v for h, v in hours.items() if h < 8 or h >= 17)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = datetime.combine(today, datetime.min.time(), tz).astimezone(timezone.utc).replace(tzinfo=None)
    today_appts = sorted([a for a in appts if today_start <= a.start_at < today_start + timedelta(days=1)], key=lambda a: a.start_at)
    upcoming = sorted([a for a in appts if a.start_at >= now and a.status == "CONFIRMED"], key=lambda a: a.start_at)[:6]
    after_hours_bookings = sum(
        1
        for a in appts
        if a.source == "ai_chat" and (a.created_at.replace(tzinfo=timezone.utc).astimezone(tz).hour < 8 or a.created_at.replace(tzinfo=timezone.utc).astimezone(tz).hour >= 17)
    )
    brand = (clinic.settings or {}).get("brand") or {}
    try:
        production_per_visit = float(brand.get("production_per_visit") or 250)
    except (TypeError, ValueError):
        production_per_visit = 250.0
    production_per_visit = max(0.0, min(production_per_visit, 50_000.0))
    estimated_production = round(n_appts * production_per_visit, 2)
    kpis["after_hours_bookings"] = after_hours_bookings
    kpis["production_per_visit"] = production_per_visit
    kpis["estimated_production"] = estimated_production

    live_lead_ids = [l.id for l in leads]
    live_appt_ids = [a.id for a in appts]
    event_q = select(AutomationEvent).order_by(AutomationEvent.created_at.desc()).limit(12)
    if not include_demo:
        clauses = []
        if live_lead_ids:
            clauses.append(AutomationEvent.lead_id.in_(live_lead_ids))
        if live_appt_ids:
            clauses.append(AutomationEvent.appointment_id.in_(live_appt_ids))
        event_q = (
            select(AutomationEvent).where(or_(*clauses) if clauses else AutomationEvent.id.is_(None))
            .order_by(AutomationEvent.created_at.desc())
            .limit(12)
        )
    recent_events = db.scalars(event_q).all()
    hot_leads = sorted(
        [l for l in leads if l.status in {"QUALIFIED", "FOLLOW_UP", "NEW", "CONTACTED"} and l.score_label in {"HOT", "WARM"}],
        key=lambda l: (l.score, l.last_activity_at),
        reverse=True,
    )[:5]

    return {
        "demo_data": include_demo,
        "demo_mode": get_settings().demo_mode,
        "period_days": DAYS,
        "kpis": kpis,
        "trends": trends,
        "series": series,
        "funnel": funnel,
        "leads_by_service": [{"name": k, "value": v} for k, v in by_service.most_common()],
        "appointment_sources": [{"name": source_labels.get(k, k), "value": v} for k, v in sources.most_common()],
        "lead_status": [{"name": k, "value": v} for k, v in status_counts.most_common()],
        "top_intents": [{"name": k, "value": v} for k, v in intents.most_common(6)],
        "after_hours_conversations": after_hours,
        "conversations_by_hour": [{"hour": h, "value": hours.get(h, 0)} for h in range(24)],
        "today_appointments": [AppointmentOut.model_validate(a).model_dump(mode="json") for a in today_appts],
        "upcoming_appointments": [AppointmentOut.model_validate(a).model_dump(mode="json") for a in upcoming],
        "recent_activity": [AutomationEventOut.model_validate(e).model_dump(mode="json") for e in recent_events],
        "hot_leads": [
            {
                "id": l.id,
                "name": l.name or "Website visitor",
                "service": l.service,
                "score": l.score,
                "score_label": l.score_label,
                "status": l.status,
                "phone": l.phone,
                "email": l.email,
            }
            for l in hot_leads
        ],
    }

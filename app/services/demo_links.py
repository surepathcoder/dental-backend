"""Demos made for one prospect: their clinic, their public facts, our assistant answering for them.

A sales tool (Kijani AI) calls ``POST /api/demo-links`` with what it has verified about a business — name,
phone, address, opening hours — and gets back a private link. The link opens a page about *their* clinic,
with the AI receptionist answering as them. Nothing is invented: no team, no prices, no insurers, no reviews;
anything we don't know is shown as a blank for them to fill. Booking works but only ever *simulates*
confirmations (``notifications.DEMO_ONLY``), and the demo expires after ``DEMO_DAYS``.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Clinic, DemoLink, Service, utcnow

DEMO_DAYS = 30
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Used only so the booking demo has times to offer when their real hours aren't known — and said so on the page.
SAMPLE_HOURS = {d: {"open": "08:00", "close": "17:00"} for d in DAY_KEYS[:5]} | {"sat": None, "sun": None}

# What a dental practice commonly offers — names and plain descriptions only, never a price.
DEMO_SERVICES = [
    ("general-dentistry", "General Dentistry", "general", 60,
     "Check-ups, cleaning, X-rays and fillings.",
     "Regular check-ups and cleaning keep small problems small. The dentist explains what they see and what, "
     "if anything, needs doing.", ["checkup", "check-up", "cleaning", "filling", "exam", "x-ray"]),
    ("cosmetic-dentistry", "Cosmetic Dentistry", "cosmetic", 60,
     "Whitening and treatments that improve how your smile looks.",
     "Whitening and other cosmetic treatments, planned with you after an examination.",
     ["whitening", "veneers", "cosmetic", "smile"]),
    ("dental-implants", "Dental Implants", "restorative", 90,
     "Replacing missing teeth.",
     "Options for replacing missing teeth, discussed after an examination and X-rays.",
     ["implant", "implants", "missing tooth", "crown", "bridge"]),
    ("invisalign", "Invisalign", "orthodontic", 60,
     "Straightening teeth.",
     "Ways to straighten teeth, discussed at a consultation.", ["braces", "aligners", "straighten", "invisalign"]),
    ("emergency-dentistry", "Emergency Dentistry", "emergency", 30,
     "Toothache, broken teeth and other urgent problems.",
     "Toothache, a broken tooth or swelling: tell us and we'll find the earliest time we can see you.",
     ["emergency", "toothache", "pain", "broken", "swelling", "urgent"]),
]


def new_token() -> str:
    return secrets.token_urlsafe(9)  # 12 characters, unguessable


def live_link(db: Session, token: str | None) -> DemoLink | None:
    if not token:
        return None
    link = db.get(DemoLink, token)
    if link is None or link.expires_at <= utcnow():
        return None
    return link


def clinic_id_for_token(db: Session, token: str | None) -> int | None:
    link = live_link(db, token)
    return link.clinic_id if link else None


def _hours(raw: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """Their weekly hours in our format, and whether they're real (False → sample hours for the booking demo)."""
    out: dict[str, Any] = {}
    for key in DAY_KEYS:
        span = (raw or {}).get(key)
        if isinstance(span, dict) and span.get("open") and span.get("close"):
            out[key] = {"open": str(span["open"])[:5], "close": str(span["close"])[:5]}
        elif key in (raw or {}):
            out[key] = None  # closed that day
    if not any(out.values()):
        return dict(SAMPLE_HOURS), False
    return {k: out.get(k) for k in DAY_KEYS}, True


def upsert_demo(db: Session, data: dict[str, Any], template: Clinic | None) -> DemoLink:
    """Create a demo for this business — or refresh the one already made for the same lead."""
    purge_expired(db)
    hours, hours_known = _hours(data.get("hours"))
    demo_meta = {
        "prepared_for": data["name"], "prepared_by": data.get("prepared_by") or "",
        "country": data.get("country") or "", "language": data.get("language") or "en",
        "website": data.get("website") or "", "hours_known": hours_known,
    }
    base_settings = dict((template.settings or {}) if template else {})
    base_settings.pop("brand", None)
    assistant = dict(base_settings.get("assistant") or {})
    assistant["greeting"] = f"Hi! I'm {assistant.get('name') or 'Ava'}, the online assistant for {data['name']}. How can I help?"

    link = None
    if data.get("external_ref"):
        link = db.scalar(select(DemoLink).where(DemoLink.external_ref == data["external_ref"],
                                                DemoLink.expires_at > utcnow()).limit(1))
    clinic = db.get(Clinic, link.clinic_id) if link else None
    if clinic is None:
        clinic = Clinic(name=data["name"], phone="", email="", address_line="", city="", state="", zip_code="")
        db.add(clinic)
    clinic.name = data["name"]
    clinic.tagline = f"Dental care in {data['city']}" if data.get("city") else "Dental care"
    clinic.phone = data.get("phone") or ""
    clinic.email = data.get("email") or ""
    clinic.address_line = data.get("address") or ""
    clinic.city = data.get("city") or ""
    clinic.state = data.get("country") or ""
    clinic.zip_code = ""
    clinic.timezone = data.get("timezone") or "UTC"
    clinic.hours = hours
    clinic.insurance_accepted = []
    clinic.financing = {}
    clinic.settings = {**base_settings, "assistant": assistant, "demo": demo_meta}
    db.flush()
    if not clinic.services:
        for order, (slug, name, category, minutes, short, description, keywords) in enumerate(DEMO_SERVICES):
            db.add(Service(clinic_id=clinic.id, slug=slug, name=name, category=category, duration_minutes=minutes,
                           short_description=short, description=description, keywords=keywords,
                           price_from=None, price_note="", is_active=True, sort_order=order))

    if link is None:
        link = DemoLink(token=new_token(), clinic_id=clinic.id, external_ref=data.get("external_ref"))
        db.add(link)
    link.prepared_by = demo_meta["prepared_by"]
    link.expires_at = utcnow() + timedelta(days=DEMO_DAYS)
    db.flush()
    return link


def purge_expired(db: Session) -> int:
    """Remove expired demos and their clinics (and with them anything a visitor typed into them)."""
    expired = db.scalars(select(DemoLink).where(DemoLink.expires_at <= utcnow())).all()
    for link in expired:
        clinic = db.get(Clinic, link.clinic_id)
        db.delete(link)
        if clinic is not None and (clinic.settings or {}).get("demo"):
            db.delete(clinic)
    return len(expired)

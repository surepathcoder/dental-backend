"""Appointment availability and booking.

Uses Cal.com (API v2) when CAL_API_KEY + CAL_EVENT_TYPE_ID are configured and
DEMO_MODE is off; otherwise a deterministic mock scheduler generates realistic
availability from the clinic's business hours and existing appointments.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Appointment, Clinic, utcnow
from ..schemas import Slot

logger = logging.getLogger("brightsmile.scheduler")

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

PROVIDERS = {
    "Dental Implants": "Dr. Marcus Reed",
    "Invisalign": "Dr. Priya Nair",
    "Cosmetic Dentistry": "Dr. Priya Nair",
    "General Dentistry": "Dr. Emily Carter",
    "Emergency Dentistry": "Dr. Emily Carter",
}

DURATIONS = {
    "General Dentistry": 60,
    "Cosmetic Dentistry": 45,
    "Dental Implants": 60,
    "Invisalign": 45,
    "Emergency Dentistry": 30,
}


@dataclass
class Preference:
    start_date: date | None = None
    end_date: date | None = None
    time_of_day: str | None = None  # morning | afternoon | evening | None
    earliest: bool = False


def provider_for(service: str) -> str:
    return PROVIDERS.get(service, "Dr. Emily Carter")


def duration_for(service: str) -> int:
    return DURATIONS.get(service, 60)


def format_day(d: datetime) -> str:
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"


def format_time(d: datetime) -> str:
    hour = d.hour % 12 or 12
    return f"{hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def to_utc_naive(local: datetime) -> datetime:
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def iso_z(dt_utc_naive: datetime) -> str:
    return dt_utc_naive.replace(microsecond=0).isoformat() + "Z"


def parse_slot_id(slot_id: str) -> datetime:
    """Slot ids are UTC ISO timestamps; returns naive UTC."""
    dt = datetime.fromisoformat(slot_id.replace("Z", "+00:00"))
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _make_slot(start_utc: datetime, minutes: int, tz: ZoneInfo, provider: str | None) -> Slot:
    local = start_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    end_utc = start_utc + timedelta(minutes=minutes)
    return Slot(
        id=iso_z(start_utc),
        start=iso_z(start_utc),
        end=iso_z(end_utc),
        date_label=format_day(local),
        time_label=format_time(local),
        label=f"{format_day(local)} · {format_time(local)}",
        provider=provider,
    )


def _is_busy(provider: str, local_start: datetime, emergency: bool) -> bool:
    """Deterministic pseudo-random calendar fill so availability looks real."""
    digest = hashlib.md5(f"{provider}|{local_start.isoformat()}".encode()).hexdigest()
    threshold = 30 if emergency else 55
    return int(digest[:4], 16) % 100 < threshold


def mock_availability(
    db: Session,
    clinic: Clinic,
    service: str,
    pref: Preference | None = None,
    limit: int | None = None,
) -> list[Slot]:
    cfg = (clinic.settings or {}).get("appointments", {})
    interval = int(cfg.get("slot_interval_minutes", 30))
    min_notice = timedelta(hours=float(cfg.get("min_notice_hours", 2)))
    window = int(cfg.get("booking_window_days", 21))
    limit = limit or int(cfg.get("slots_to_offer", 6))
    emergency = service == "Emergency Dentistry"
    if emergency:
        min_notice = timedelta(minutes=45)

    tz = ZoneInfo(clinic.timezone)
    now_local = datetime.now(tz)
    earliest_local = now_local + min_notice
    provider = provider_for(service)
    minutes = duration_for(service)
    pref = pref or Preference()

    start_day = max(now_local.date(), pref.start_date or now_local.date())
    end_day = pref.end_date or (now_local.date() + timedelta(days=window))
    end_day = min(end_day, now_local.date() + timedelta(days=window))

    # Existing confirmed appointments for this provider (overlap check)
    range_start = to_utc_naive(datetime.combine(start_day, time.min, tz))
    range_end = to_utc_naive(datetime.combine(end_day + timedelta(days=1), time.min, tz))
    booked = db.scalars(
        select(Appointment).where(
            Appointment.clinic_id == clinic.id,
            Appointment.status == "CONFIRMED",
            Appointment.start_at < range_end,
            Appointment.end_at > range_start,
        )
    ).all()
    booked_spans = [(a.start_at, a.end_at) for a in booked if (a.provider_name or provider) == provider]

    slots: list[Slot] = []
    day = start_day
    while day <= end_day and len(slots) < limit:
        hours = (clinic.hours or {}).get(DAY_KEYS[day.weekday()])
        if hours:
            open_t = time.fromisoformat(hours["open"])
            close_t = time.fromisoformat(hours["close"])
            cursor = datetime.combine(day, open_t, tz)
            close_dt = datetime.combine(day, close_t, tz)
            day_count = 0
            while cursor + timedelta(minutes=minutes) <= close_dt and len(slots) < limit:
                local = cursor
                cursor += timedelta(minutes=interval)
                if local < earliest_local:
                    continue
                if time(12, 0) <= local.time() < time(13, 0) and day.weekday() < 5:
                    continue  # lunch
                if pref.time_of_day == "morning" and local.hour >= 12:
                    continue
                if pref.time_of_day == "afternoon" and not (12 <= local.hour < 17):
                    continue
                if pref.time_of_day == "evening" and local.hour < 15:
                    continue
                if _is_busy(provider, local, emergency):
                    continue
                start_utc = to_utc_naive(local)
                end_utc = start_utc + timedelta(minutes=minutes)
                if any(s < end_utc and e > start_utc for s, e in booked_spans):
                    continue
                slots.append(_make_slot(start_utc, minutes, tz, provider))
                day_count += 1
                # Spread offers across days unless the patient wants the earliest time
                if not pref.earliest and not emergency and day_count >= 3:
                    break
        day += timedelta(days=1)
    return slots


# ── Cal.com ──────────────────────────────────────────────────────────────────
def _cal_headers(version: str) -> dict[str, str]:
    s = get_settings()
    return {"Authorization": f"Bearer {s.cal_api_key}", "cal-api-version": version}


def calcom_availability(clinic: Clinic, service: str, pref: Preference | None, limit: int = 6) -> list[Slot] | None:
    s = get_settings()
    tz = ZoneInfo(clinic.timezone)
    today = datetime.now(tz).date()
    pref = pref or Preference()
    start = max(today, pref.start_date or today)
    end = pref.end_date or (start + timedelta(days=14))
    try:
        resp = httpx.get(
            f"{s.cal_api_url}/slots",
            params={
                "eventTypeId": s.cal_event_type_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timeZone": clinic.timezone,
            },
            headers=_cal_headers("2024-09-04"),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        slots: list[Slot] = []
        for _day, entries in sorted(data.items()):
            for entry in entries:
                start_dt = datetime.fromisoformat(entry["start"].replace("Z", "+00:00"))
                local = start_dt.astimezone(tz)
                if pref.time_of_day == "morning" and local.hour >= 12:
                    continue
                if pref.time_of_day == "afternoon" and not (12 <= local.hour < 17):
                    continue
                slots.append(_make_slot(to_utc_naive(start_dt), duration_for(service), tz, provider_for(service)))
                if len(slots) >= limit:
                    return slots
        return slots
    except Exception as exc:
        logger.warning("Cal.com availability failed, falling back to mock: %s", exc)
        return None


def calcom_book(clinic: Clinic, start_utc: datetime, name: str, email: str, phone: str | None, service: str) -> str | None:
    s = get_settings()
    try:
        resp = httpx.post(
            f"{s.cal_api_url}/bookings",
            json={
                "start": iso_z(start_utc),
                "eventTypeId": int(s.cal_event_type_id),
                "attendee": {"name": name, "email": email, "timeZone": clinic.timezone, **({"phoneNumber": phone} if phone else {})},
                "metadata": {"service": service, "source": "ai_receptionist"},
            },
            headers=_cal_headers("2024-08-13"),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return str(data.get("uid") or data.get("id") or "")
    except Exception as exc:
        logger.warning("Cal.com booking failed: %s", exc)
        return None


# ── Public API ───────────────────────────────────────────────────────────────
def scheduler_name() -> str:
    return "calcom" if get_settings().calendar_live else "mock"


def get_availability(db: Session, clinic: Clinic, service: str, pref: Preference | None = None, limit: int | None = None) -> list[Slot]:
    if get_settings().calendar_live:
        slots = calcom_availability(clinic, service, pref, limit or 6)
        if slots is not None:
            return slots
    return mock_availability(db, clinic, service, pref, limit)


def validate_slot(db: Session, clinic: Clinic, service: str, slot_id: str) -> datetime:
    """Ensure the requested slot is genuinely available. Returns naive UTC start."""
    try:
        start_utc = parse_slot_id(slot_id)
    except ValueError as exc:
        raise ValueError("Invalid time slot") from exc
    if start_utc <= utcnow():
        raise ValueError("That time has already passed")
    tz = ZoneInfo(clinic.timezone)
    local_day = start_utc.replace(tzinfo=timezone.utc).astimezone(tz).date()
    slots = get_availability(db, clinic, service, Preference(start_date=local_day, end_date=local_day, earliest=True), limit=40)
    if slot_id not in {s.id for s in slots}:
        raise ValueError("Sorry — that time was just taken. Please choose another slot.")
    return start_utc

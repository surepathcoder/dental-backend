"""Lead scoring and status transitions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Lead, utcnow

TERMINAL = {"BOOKED", "CONVERTED", "LOST"}


def score_lead(lead: Lead) -> tuple[int, str]:
    if lead.status in {"BOOKED", "CONVERTED"}:
        return 100, "HOT"
    score = 0
    score += 20 if lead.service else 0
    score += 15 if lead.name else 0
    score += 15 if lead.phone else 0
    score += 10 if lead.email else 0
    score += 5 if lead.insurance else 0
    score += 10 if lead.notes and "Member ID:" in lead.notes else 0
    score += 5 if lead.preferred_date or lead.preferred_time else 0
    score += {"emergency": 20, "soon": 15, "normal": 5}.get(lead.urgency or "", 0)
    score = min(score, 95)
    label = "HOT" if score >= 80 else "WARM" if score >= 50 else "COLD"
    return score, label


def is_qualified(lead: Lead) -> bool:
    return bool(lead.name and (lead.phone or lead.email) and lead.service)


def refresh_lead(lead: Lead) -> None:
    """Recompute score and move NEW → QUALIFIED when enough info exists.
    Manually set statuses (e.g. CONTACTED) are never overridden."""
    if lead.status == "NEW" and is_qualified(lead):
        lead.status = "QUALIFIED"
    lead.score, lead.score_label = score_lead(lead)
    lead.last_activity_at = utcnow()


def first_name(lead: Lead | None) -> str:
    if lead and lead.name:
        return lead.name.split()[0]
    return "there"


def get_or_create_lead(db: Session, clinic_id: int, lead_id: int | None, source: str = "ai_chat") -> Lead:
    if lead_id:
        lead = db.get(Lead, lead_id)
        if lead:
            return lead
    lead = Lead(clinic_id=clinic_id, source=source, status="NEW")
    db.add(lead)
    db.flush()
    return lead

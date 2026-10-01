from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..database import get_db
from ..models import WaitlistEntry
from ..schemas import WaitlistCreate, WaitlistOut, WaitlistUpdate
from ..security import audit, client_ip, rate_limit, require_admin

router = APIRouter(prefix="/api/waitlist", tags=["Waitlist"])


@router.get("", response_model=list[WaitlistOut], dependencies=[Depends(require_admin)], summary="List waitlist entries")
def list_waitlist(db: Session = Depends(get_db)) -> list[WaitlistEntry]:
    return list(db.scalars(select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc())).all())


@router.post("", response_model=WaitlistOut, status_code=201, dependencies=[Depends(rate_limit("waitlist", 8))],
             summary="Join the waitlist")
def join_waitlist(body: WaitlistCreate, request: Request, db: Session = Depends(get_db)) -> WaitlistEntry:
    clinic = load_clinic(db)
    entry = WaitlistEntry(
        clinic_id=clinic.id,
        name=body.name,
        phone=body.phone,
        email=str(body.email) if body.email else None,
        service=body.service,
        notes=body.notes or "",
        status="WAITING",
    )
    db.add(entry)
    audit(db, "public", "waitlist.joined", "waitlist", None, ip=client_ip(request), detail={"service": body.service})
    db.commit()
    db.refresh(entry)
    return entry


@router.patch("/{entry_id}", response_model=WaitlistOut, dependencies=[Depends(require_admin)])
def update_waitlist(entry_id: int, body: WaitlistUpdate, request: Request, db: Session = Depends(get_db)) -> WaitlistEntry:
    entry = db.get(WaitlistEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Waitlist entry not found")
    if body.status:
        entry.status = body.status
    if body.notes is not None:
        entry.notes = body.notes
    audit(db, "admin", "waitlist.updated", "waitlist", entry.id, ip=client_ip(request), detail={"status": entry.status})
    db.commit()
    db.refresh(entry)
    return entry

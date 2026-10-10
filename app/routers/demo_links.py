"""Private demo links for prospects (see services/demo_links.py)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..database import get_db
from ..models import DemoLink, utcnow
from ..schemas import ClinicOut, clean_text
from ..security import audit, client_ip, rate_limit, require_admin
from ..services.demo_links import live_link, upsert_demo

router = APIRouter(prefix="/api/demo-links", tags=["Demo links"])


class DemoCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=60)
    # {"mon": {"open": "08:00", "close": "17:00"} | null, ...}; leave out when unknown
    hours: dict[str, Any] | None = None
    website: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default="en", pattern=r"^(en|sw)$")
    prepared_by: str | None = Field(default=None, max_length=160)
    external_ref: str | None = Field(default=None, max_length=80)

    @field_validator("name", "phone", "email", "address", "city", "country", "website", "prepared_by")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v, 200)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if not v:
            return None
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError("unknown time zone") from None
        return v


@router.post("", dependencies=[Depends(require_admin)], summary="Make (or refresh) a demo for one prospect")
def create_demo(body: DemoCreate, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    link = upsert_demo(db, body.model_dump(), load_clinic(db))
    audit(db, "admin", "demo_link.saved", "demo_link", link.token, ip=client_ip(request),
          detail={"external_ref": body.external_ref})
    db.commit()
    return {"token": link.token, "path": f"/d/{link.token}", "expires_at": link.expires_at.isoformat() + "Z",
            "views": link.views}


@router.get("/{token}", dependencies=[Depends(rate_limit("demo_link", 60))],
            summary="A demo's clinic, for the private demo page")
def get_demo(token: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    link = live_link(db, token if len(token) <= 40 else None)
    if link is None:
        raise HTTPException(404, "This demo link has expired or doesn't exist.")
    link.views = (link.views or 0) + 1
    link.last_viewed_at = utcnow()
    clinic = load_clinic(db, link.clinic_id)
    db.commit()
    meta = dict((clinic.settings or {}).get("demo") or {})
    return {"clinic": ClinicOut.model_validate(clinic).model_dump(mode="json"),
            "demo": {**meta, "expires_at": link.expires_at.isoformat() + "Z"}}


@router.get("/{token}/stats", dependencies=[Depends(require_admin)], summary="How often a demo was opened")
def demo_stats(token: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    link = db.get(DemoLink, token)
    if link is None:
        raise HTTPException(404, "No such demo link.")
    return {"views": link.views, "last_viewed_at": link.last_viewed_at.isoformat() + "Z" if link.last_viewed_at else None,
            "expires_at": link.expires_at.isoformat() + "Z"}

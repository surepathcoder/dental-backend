"""Demo helpers used by the admin UI to walk prospects through the product."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..agent.runner import handle_chat
from ..config import get_settings
from ..database import get_db
from ..models import Conversation
from ..schemas import ChatAction, ChatRequest
from ..security import audit, client_ip, limiter, require_admin
from ..seed import reset_demo
from ..services.runtime_settings import apply_overrides

router = APIRouter(prefix="/api/demo", tags=["Demo"], dependencies=[Depends(require_admin)])


@router.post("/reset", summary="Reset the database to the seeded demo dataset")
def reset(request: Request, db: Session = Depends(get_db)) -> dict:
    if not get_settings().demo_mode:
        raise HTTPException(403, "Reset is only available when DEMO_MODE=true")
    reset_demo(db)
    apply_overrides(db)
    limiter.reset()
    audit(db, "admin", "demo.reset", ip=client_ip(request))
    db.commit()
    return {"ok": True}


@router.post("/missed-lead", summary="Simulate a visitor who shows interest but leaves without booking")
def simulate_missed_lead(request: Request, db: Session = Depends(get_db)) -> dict:
    """Plays a short scripted conversation through the real AI workflow."""
    ip = client_ip(request)
    r1 = handle_chat(db, ChatRequest(message="Do you offer dental implants?", page="/services/dental-implants"), ip)
    cid = r1.conversation_id
    handle_chat(db, ChatRequest(conversation_id=cid, message="Not right now, maybe later."), ip)
    r3 = handle_chat(db, ChatRequest(conversation_id=cid, message="", action=ChatAction(
        type="submit_contact", name="Morgan Ellis", phone="(512) 555-0163", email="morgan.ellis@example.com")), ip)
    convo = db.get(Conversation, cid)
    return {"conversation_id": cid, "lead_id": convo.lead_id if convo else None, "reply": r3.reply, "events": [e.model_dump() for e in r3.events]}

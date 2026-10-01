"""Public Twilio webhooks for inbound SMS confirm/reschedule and missed-call texts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response

from ..agent.runner import load_clinic
from ..database import get_db
from ..security import rate_limit
from ..services import automations
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


def _twiml(message: str) -> Response:
    safe = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'
    return Response(content=xml, media_type="application/xml")


@router.post("/twilio/sms", dependencies=[Depends(rate_limit("twilio_sms", 30))])
def twilio_sms(
    request: Request,
    db: Session = Depends(get_db),
    From: str = Form(default=""),
    Body: str = Form(default=""),
) -> Response:
    clinic = load_clinic(db)
    reply = automations.handle_inbound_sms(db, clinic, From, Body)
    db.commit()
    return _twiml(reply)


@router.post("/twilio/voice", dependencies=[Depends(rate_limit("twilio_voice", 20))])
def twilio_voice(
    db: Session = Depends(get_db),
    From: str = Form(default=""),
    CallStatus: str = Form(default=""),
) -> Response:
    clinic = load_clinic(db)
    if CallStatus.lower() in {"no-answer", "busy", "failed", "canceled"}:
        automations.handle_missed_call(db, clinic, From)
        db.commit()
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")

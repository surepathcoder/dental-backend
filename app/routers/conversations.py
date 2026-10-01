from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Conversation, Lead, Message, utcnow
from ..schemas import ConversationDetail, ConversationOut, MessageOut, StaffReplyIn
from ..security import audit, client_ip, require_admin

router = APIRouter(prefix="/api/conversations", tags=["Conversations"], dependencies=[Depends(require_admin)])


def _brief(convo: Conversation) -> str | None:
    return (convo.state or {}).get("staff_brief")


@router.get("", response_model=dict, summary="List AI conversations")
def list_conversations(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, pattern=r"^(ACTIVE|BOOKED|FOLLOW_UP|ABANDONED|CLOSED|WAITING_STAFF)$"),
    score: str | None = Query(default=None, pattern=r"^(HOT|WARM|COLD)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    stmt = select(Conversation).where(Conversation.is_demo.is_(False))
    if status:
        stmt = stmt.where(Conversation.status == status)
    if score:
        stmt = stmt.where(Conversation.lead_score == score)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    convos = db.scalars(stmt.order_by(Conversation.last_message_at.desc()).limit(limit).offset(offset)).all()
    ids = [c.id for c in convos]
    counts = dict(db.execute(select(Message.conversation_id, func.count(Message.id)).where(Message.conversation_id.in_(ids)).group_by(Message.conversation_id)).all()) if ids else {}
    first_msgs: dict[str, str] = {}
    if ids:
        sub = select(Message.conversation_id, func.min(Message.id).label("mid")).where(Message.conversation_id.in_(ids), Message.role == "user").group_by(Message.conversation_id).subquery()
        for cid, content in db.execute(select(Message.conversation_id, Message.content).join(sub, Message.id == sub.c.mid)).all():
            first_msgs[cid] = content
    lead_names = dict(db.execute(select(Lead.id, Lead.name).where(Lead.id.in_([c.lead_id for c in convos if c.lead_id]))).all())
    items = [
        ConversationOut.model_validate(c).model_copy(update={
            "patient_name": lead_names.get(c.lead_id) if c.lead_id else None,
            "message_count": counts.get(c.id, 0),
            "preview": (first_msgs.get(c.id) or "")[:140],
            "staff_brief": _brief(c),
        }).model_dump(mode="json")
        for c in convos
    ]
    status_counts = dict(db.execute(select(Conversation.status, func.count(Conversation.id)).where(Conversation.is_demo.is_(False)).group_by(Conversation.status)).all())
    return {"items": items, "total": total, "status_counts": status_counts}


@router.get("/{conversation_id}", response_model=ConversationDetail, summary="Conversation transcript")
def get_conversation(conversation_id: str, db: Session = Depends(get_db)) -> ConversationDetail:
    convo = db.scalar(select(Conversation).where(Conversation.id == conversation_id).options(selectinload(Conversation.messages)))
    if convo is None:
        raise HTTPException(404, "Conversation not found")
    lead = db.get(Lead, convo.lead_id) if convo.lead_id else None
    return ConversationDetail.model_validate(convo).model_copy(update={
        "patient_name": lead.name if lead else None,
        "message_count": len(convo.messages),
        "staff_brief": _brief(convo),
        "messages": [MessageOut.model_validate(m) for m in convo.messages],
    })


@router.post("/{conversation_id}/staff-reply", response_model=MessageOut, summary="Reply as front desk in a live chat")
def staff_reply(conversation_id: str, body: StaffReplyIn, request: Request, db: Session = Depends(get_db)) -> Message:
    convo = db.get(Conversation, conversation_id)
    if convo is None:
        raise HTTPException(404, "Conversation not found")
    convo.status = "WAITING_STAFF"
    convo.stage = "waiting_staff"
    convo.last_message_at = utcnow()
    state = dict(convo.state or {})
    fields = dict(state.get("fields") or {})
    if not fields.get("waiting_since"):
        fields["waiting_since"] = utcnow().isoformat()
    state["fields"] = fields
    state["stage"] = "waiting_staff"
    convo.state = state
    msg = Message(
        conversation_id=convo.id,
        role="assistant",
        content=body.message,
        meta={"source": "staff", "intent": "human", "stage": "waiting_staff", "trace": ["staff_reply"]},
    )
    db.add(msg)
    audit(db, "admin", "conversation.staff_reply", "conversation", convo.id, ip=client_ip(request))
    db.commit()
    db.refresh(msg)
    return msg


@router.post("/{conversation_id}/resume", response_model=ConversationDetail, summary="Return the conversation to the AI receptionist")
def resume_ai(conversation_id: str, request: Request, db: Session = Depends(get_db)) -> ConversationDetail:
    convo = db.scalar(select(Conversation).where(Conversation.id == conversation_id).options(selectinload(Conversation.messages)))
    if convo is None:
        raise HTTPException(404, "Conversation not found")
    convo.status = "ACTIVE" if convo.status == "WAITING_STAFF" else convo.status
    convo.stage = "start"
    state = dict(convo.state or {})
    fields = dict(state.get("fields") or {})
    fields.pop("waiting_since", None)
    state["fields"] = fields
    state["stage"] = "start"
    convo.state = state
    convo.last_message_at = utcnow()
    db.add(Message(
        conversation_id=convo.id,
        role="assistant",
        content="I'm back — our team asked me to keep helping. What else can I do for you?",
        meta={"source": "ai", "intent": "greeting", "stage": "start", "trace": ["staff_resume"], "quick_replies": ["Book an appointment", "What are your hours?"]},
    ))
    audit(db, "admin", "conversation.resume_ai", "conversation", convo.id, ip=client_ip(request))
    db.commit()
    db.refresh(convo)
    lead = db.get(Lead, convo.lead_id) if convo.lead_id else None
    return ConversationDetail.model_validate(convo).model_copy(update={
        "patient_name": lead.name if lead else None,
        "message_count": len(convo.messages),
        "staff_brief": _brief(convo),
        "messages": [MessageOut.model_validate(m) for m in convo.messages],
    })

"""Runs one chat turn: loads conversation state, invokes the LangGraph
workflow, persists messages / lead / conversation, and builds the response."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import Clinic, Conversation, Message, utcnow
from ..schemas import ChatRequest, ChatResponse, ChatSource, FlowEvent, LeadSummary, Slot
from ..security import audit
from ..services import llm
from ..services.automations import end_conversation
from .graph import STAFF_WAIT_MINUTES, TurnContext, receptionist_graph
from .knowledge import build_docs
from .memory import session_expired

logger = logging.getLogger("brightsmile.chat")


def load_clinic(db: Session, clinic_id: int | None = None) -> Clinic:
    """The practice's own clinic (the first one), or — for a prospect's demo — that demo's clinic."""
    stmt = select(Clinic).options(
        selectinload(Clinic.services),
        selectinload(Clinic.faqs),
        selectinload(Clinic.knowledge_documents),
    )
    stmt = stmt.where(Clinic.id == clinic_id) if clinic_id else stmt.order_by(Clinic.id)
    clinic = db.scalar(stmt.limit(1))
    if clinic is None:
        raise RuntimeError("Clinic not seeded")
    return clinic


def _describe_action(req: ChatRequest, offered: list[dict]) -> str:
    action = req.action
    if not action:
        return req.message
    if action.type == "select_slot":
        label = next((s["label"] for s in offered if s["id"] == action.slot_id), action.slot_id)
        return f"I'll take {label}"
    if action.type == "submit_contact":
        bits = [b for b in (action.name, action.phone, action.email) if b]
        return "My details: " + " · ".join(bits)
    if action.type == "cancel_appointment":
        return "Please cancel my appointment"
    if action.type == "reschedule_slot":
        label = next((s["label"] for s in offered if s["id"] == action.slot_id), action.slot_id)
        return f"Please move my appointment to {label}"
    return "Start over"


def _staff_brief(fields: dict, appointment: dict | None, intent: str, topic: str | None) -> str:
    name = fields.get("name") or "Website visitor"
    service = fields.get("service") or topic or "a visit"
    bits = [f"{name} is interested in {service}"]
    if fields.get("patient_type"):
        bits.append(f"{fields['patient_type']} patient")
    if fields.get("insurance"):
        bits.append(f"insurance: {fields['insurance']}")
    if fields.get("urgency"):
        bits.append(f"urgency: {fields['urgency']}")
    if appointment:
        bits.append(f"booked {appointment.get('date')} at {appointment.get('time')} with {appointment.get('provider')}")
    elif intent == "human":
        bits.append("asked to speak with the front desk")
    return ". ".join(bits) + "."


def _maybe_polish_brief(brief: str) -> str:
    if not llm.is_available():
        return brief
    polished = llm.chat_completion(
        [
            {
                "role": "system",
                "content": "Rewrite this dental front-desk handoff as 2 short sentences. No diagnosis. No extra facts.",
            },
            {"role": "user", "content": brief},
        ],
        max_tokens=80,
        temperature=0.1,
    )
    return polished or brief


def _to_response(
    *,
    convo: Conversation,
    reply: str,
    quick: list[str],
    ui,
    slots: list[Slot],
    appointment,
    events: list[FlowEvent],
    lead,
    stage: str,
    intent: str,
    disclaimer: str | None,
    sources: list[ChatSource],
    settings,
    last_message_id: int | None = None,
    new_session: bool = False,
) -> ChatResponse:
    waiting = convo.status == "WAITING_STAFF"
    brief = (convo.state or {}).get("staff_brief")
    return ChatResponse(
        conversation_id=convo.id,
        reply=reply,
        quick_replies=quick,
        ui=ui,
        slots=slots,
        appointment=appointment,
        events=events,
        lead=LeadSummary(
            id=lead.id, name=lead.name, phone=lead.phone, email=lead.email, service=lead.service,
            patient_type=lead.patient_type, insurance=lead.insurance, urgency=lead.urgency,
            preferred_date=lead.preferred_date, preferred_time=lead.preferred_time,
            status=lead.status, score_label=lead.score_label,
        ) if lead else None,
        stage=stage,
        intent=intent,
        disclaimer=disclaimer,
        demo_mode=settings.demo_mode,
        ai_provider=llm.provider_label(),
        sources=sources,
        waiting_staff=waiting,
        staff_brief=brief,
        last_message_id=last_message_id,
        new_session=new_session,
    )


def _waiting_expired(convo: Conversation) -> bool:
    raw = (convo.state or {}).get("fields", {}).get("waiting_since")
    if not raw:
        return False
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return utcnow() - started > timedelta(minutes=STAFF_WAIT_MINUTES)


def _session_should_roll(convo: Conversation) -> bool:
    """Idle working memory expired — start a new visit instead of resuming a stale flow."""
    if not session_expired(convo):
        return False
    if convo.status == "WAITING_STAFF" and not _waiting_expired(convo):
        return False
    return True


def handle_waiting_staff(db: Session, convo: Conversation, req: ChatRequest) -> ChatResponse | None:
    """Park the LangGraph bot while a human owns the thread, unless the wait timed out."""
    if convo.status != "WAITING_STAFF":
        return None
    if req.action and req.action.type == "restart":
        return None
    if _waiting_expired(convo):
        convo.status = "ACTIVE"
        state = dict(convo.state or {})
        fields = dict(state.get("fields") or {})
        fields.pop("waiting_since", None)
        state["fields"] = fields
        state["stage"] = "start"
        convo.state = state
        convo.stage = "start"
        return None

    clinic = load_clinic(db, convo.clinic_id)
    settings = get_settings()
    user_text = req.message or "..."
    db.add(Message(conversation_id=convo.id, role="user", content=user_text[:1000], meta={"waiting_staff": True}))
    reply = (
        "I've passed that along to the front desk — they'll see it in this chat. "
        f"If you need someone right away, call {clinic.phone}."
    )
    ack = Message(
        conversation_id=convo.id,
        role="assistant",
        content=reply,
        meta={"intent": "human", "stage": "waiting_staff", "trace": ["staff_handoff"], "source": "queued"},
    )
    db.add(ack)
    convo.last_message_at = utcnow()
    db.commit()
    db.refresh(ack)
    return _to_response(
        convo=convo, reply=reply, quick=["I'll wait"], ui=None, slots=[], appointment=None,
        events=[], lead=None, stage="waiting_staff", intent="human", disclaimer=None,
        sources=[], settings=settings, last_message_id=ack.id,
    )


def handle_chat(db: Session, req: ChatRequest, ip: str | None = None) -> ChatResponse:
    from ..services.notifications import DEMO_ONLY

    token = DEMO_ONLY.set(False)
    try:
        return _handle_chat(db, req, ip)
    finally:
        DEMO_ONLY.reset(token)


def _handle_chat(db: Session, req: ChatRequest, ip: str | None = None) -> ChatResponse:
    settings = get_settings()
    from ..services.demo_links import clinic_id_for_token

    convo = db.get(Conversation, req.conversation_id) if req.conversation_id else None
    # A demo link's chat talks as that clinic; an existing conversation keeps the clinic it started with.
    clinic = load_clinic(db, convo.clinic_id if convo else (clinic_id_for_token(db, req.demo) if req.demo else None))
    from ..services.notifications import DEMO_ONLY

    DEMO_ONLY.set(bool((clinic.settings or {}).get("demo")))
    is_new = convo is None
    new_session = False
    if convo is None:
        convo = Conversation(clinic_id=clinic.id, page=req.page, is_demo=False, state={})
        db.add(convo)
        db.flush()
        audit(db, "public", "conversation.started", "conversation", convo.id, ip=ip)
    elif _session_should_roll(convo):
        if convo.status == "ACTIVE":
            end_conversation(db, clinic, convo, reason="Chat session expired")
        convo = Conversation(clinic_id=clinic.id, page=req.page, is_demo=False, state={})
        db.add(convo)
        db.flush()
        audit(db, "public", "conversation.started", "conversation", convo.id, ip=ip, detail={"reason": "session_expired"})
        is_new = True
        new_session = True
    elif convo.status in {"ABANDONED", "CLOSED", "FOLLOW_UP"}:
        convo.status = "ACTIVE"
    elif req.page and not convo.page:
        convo.page = req.page

    parked = handle_waiting_staff(db, convo, req)
    if parked:
        return parked

    state = convo.state or {}
    offered = state.get("offered_slots", [])
    user_text = _describe_action(req, offered)
    db.add(Message(conversation_id=convo.id, role="user", content=user_text[:1000], meta={"action": req.action.model_dump() if req.action else None}))
    db.flush()

    history = [
        {"role": m.role, "content": m.content}
        for m in db.scalars(select(Message).where(Message.conversation_id == convo.id).order_by(Message.id.desc()).limit(16)).all()
    ][::-1]

    services = [{"name": s.name, "slug": s.slug, "keywords": s.keywords or []} for s in clinic.services if s.is_active]
    ctx = TurnContext(db=db, clinic=clinic, conversation=convo, services=services, docs=build_docs(clinic))

    result = receptionist_graph.invoke(
        {
            "message": req.message,
            "action": req.action.model_dump() if req.action else None,
            "history": history,
            "stage": state.get("stage", "start"),
            "fields": state.get("fields", {}),
            "offered_slots": offered,
            "page": req.page or convo.page,
            "reply_parts": [],
            "events": [],
            "trace": [],
            "sources": [],
        },
        config={"configurable": {"ctx": ctx}},
    )

    fields = result.get("fields", {})
    stage = result.get("stage", "start")
    intent = result.get("intent", "unknown")

    has_contact = bool(fields.get("phone") or fields.get("email"))
    lead = ctx.sync_lead(fields, create=has_contact)
    if lead and is_new is False and lead.id and convo.lead_id is None:
        convo.lead_id = lead.id

    reply = " ".join(p for p in result.get("reply_parts", []) if p).strip() or "How else can I help?"
    topic = result.get("topic")
    convo.state = {
        "stage": stage,
        "fields": fields,
        "offered_slots": result.get("offered_slots", offered if stage == "choose_slot" else []),
        "staff_brief": state.get("staff_brief"),
    }
    convo.stage = stage
    if topic:
        convo.intent = topic
    if fields.get("service"):
        convo.service = fields["service"]
    convo.lead_score = lead.score_label if lead else "COLD"
    convo.last_message_at = utcnow()

    if result.get("appointment") or intent == "human" or stage == "waiting_staff":
        brief = _maybe_polish_brief(_staff_brief(fields, result.get("appointment"), intent, topic))
        convo.state = {**convo.state, "staff_brief": brief}

    events = [FlowEvent(**e) for e in result.get("events", [])]
    slots = [Slot(**s) for s in result.get("slots", [])] if result.get("slots") else []
    ui = result.get("ui")
    quick = result.get("quick_replies", [])
    sources = [ChatSource(id=s.get("id"), title=s["title"]) for s in result.get("sources", []) if s.get("title")]
    assistant_msg = Message(
        conversation_id=convo.id,
        role="assistant",
        content=reply,
        meta={
            "intent": intent,
            "stage": stage,
            "trace": result.get("trace", []),
            "quick_replies": quick,
            "ui": ui,
            "slots": [s.model_dump() for s in slots],
            "events": [e.model_dump() for e in events],
            "retrieved": [r["title"] for r in result.get("retrieved", [])],
            "sources": [s.model_dump() for s in sources],
            "disclaimer": result.get("disclaimer"),
        },
    )
    db.add(assistant_msg)
    if result.get("appointment"):
        audit(db, "ai_receptionist", "appointment.booked", "appointment", result["appointment"]["id"], ip=ip,
              detail={"service": result["appointment"]["service"], "demo": result["appointment"]["demo"]})
    db.commit()
    db.refresh(assistant_msg)

    return _to_response(
        convo=convo, reply=reply, quick=quick, ui=ui, slots=slots, appointment=result.get("appointment"),
        events=events, lead=lead, stage=stage, intent=intent, disclaimer=result.get("disclaimer"),
        sources=sources, settings=settings, last_message_id=assistant_msg.id,
        new_session=new_session,
    )


async def sse_events(response: ChatResponse):
    """Yield SSE chunks with a short pause so the chat bubble types out."""
    text = response.reply or ""
    size = 18
    for i in range(0, len(text), size):
        yield f"data: {json.dumps({'delta': text[i : i + size]})}\n\n"
        await asyncio.sleep(0.024)
    payload = response.model_dump(mode="json")
    payload["reply"] = response.reply
    yield f"event: done\ndata: {json.dumps(payload)}\n\n"


def conversation_updates(db: Session, conversation_id: str, after_id: int = 0) -> dict:
    convo = db.get(Conversation, conversation_id)
    if convo is None:
        return {}
    messages = list(
        db.scalars(
            select(Message).where(Message.conversation_id == conversation_id, Message.id > after_id).order_by(Message.id)
        ).all()
    )
    last_id = messages[-1].id if messages else after_id
    return {
        "conversation_id": convo.id,
        "status": convo.status,
        "stage": convo.stage,
        "waiting_staff": convo.status == "WAITING_STAFF",
        "last_id": last_id,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "source": (m.meta or {}).get("source"),
                "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
            }
            for m in messages
        ],
    }

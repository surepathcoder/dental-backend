from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..agent.runner import conversation_updates, handle_chat, load_clinic, sse_events
from ..config import get_settings
from ..database import get_db
from ..models import Conversation
from ..schemas import ChatRequest, ChatResponse, FlowEvent
from ..security import client_ip, rate_limit
from ..services.automations import end_conversation

router = APIRouter(prefix="/api/chat", tags=["AI Receptionist"])
settings = get_settings()


@router.post(
    "",
    response_model=ChatResponse,
    dependencies=[Depends(rate_limit("chat", settings.chat_rate_limit_per_minute))],
    summary="Send a message to the AI receptionist",
)
def chat(req: ChatRequest, request: Request, db: Session = Depends(get_db)) -> ChatResponse:
    """Runs one turn of the LangGraph receptionist workflow.

    Omit `conversation_id` to start a new conversation. Use `action` for
    structured UI events (selecting a slot, submitting the contact card).
    """
    return handle_chat(db, req, ip=client_ip(request))


@router.post(
    "/stream",
    dependencies=[Depends(rate_limit("chat", settings.chat_rate_limit_per_minute))],
    summary="Stream a receptionist reply (SSE)",
)
def chat_stream(req: ChatRequest, request: Request, db: Session = Depends(get_db)) -> StreamingResponse:
    """Same as POST /api/chat, then streams the assembled reply as SSE tokens.

    Events: `data: {"delta": "..."}` followed by `event: done` with the full ChatResponse.
    """
    resp = handle_chat(db, req, ip=client_ip(request))
    return StreamingResponse(
        sse_events(resp),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{conversation_id}/updates",
    dependencies=[Depends(rate_limit("chat_poll", 60))],
    summary="Poll new messages (used while waiting for staff)",
)
def chat_updates(
    conversation_id: str,
    db: Session = Depends(get_db),
    after_id: int = Query(default=0, ge=0),
) -> dict:
    if len(conversation_id) != 32 or not conversation_id.isalnum():
        raise HTTPException(400, "Invalid conversation id")
    data = conversation_updates(db, conversation_id, after_id)
    if not data:
        raise HTTPException(404, "Conversation not found")
    return data


@router.post(
    "/{conversation_id}/end",
    response_model=FlowEvent | None,
    dependencies=[Depends(rate_limit("chat_end", 20))],
    summary="Visitor closed the chat — triggers missed-lead follow-up if applicable",
)
def end_chat(conversation_id: str, db: Session = Depends(get_db)) -> FlowEvent | None:
    if len(conversation_id) != 32 or not conversation_id.isalnum():
        raise HTTPException(400, "Invalid conversation id")
    convo = db.get(Conversation, conversation_id)
    if convo is None:
        raise HTTPException(404, "Conversation not found")
    event = end_conversation(db, load_clinic(db), convo, reason="Visitor closed chat without booking")
    db.commit()
    return event

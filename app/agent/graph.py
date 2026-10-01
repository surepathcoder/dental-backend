"""LangGraph workflow for the AI receptionist.

                         ┌──────────────┐
                START ──▶│detect_intent │
                         └──────┬───────┘
     ┌──────────────┬───────────┼─────────────┬──────────────┐
     ▼              ▼           ▼             ▼              ▼
 emergency      retrieve     qualify        book         followup
     │              │           │             │              │
     └──▶ qualify   ▼           ├─▶ availability ─▶ END     END
                 respond ─▶ END │             ▼
                                END         confirm ─▶ schedule_followups ─▶ END

The booking flow itself is deterministic (the LLM never invents slots,
prices or confirmations). The LLM is used for grounded answers to free-form
questions and as a fallback intent classifier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Appointment, Clinic, Conversation, Lead, utcnow
from ..schemas import normalize_us_phone
from ..services import automations, leads as lead_svc, llm, scheduler
from . import memory as memory_svc
from . import nlu
from . import locale as locale_svc
from .knowledge import (
    Doc,
    after_hours_note,
    doc_for_service_slug,
    insurance_specific_answer,
    retrieve,
    service_price_catalog,
    service_slug_from_page,
)

EMERGENCY_SAFETY = (
    "If you are experiencing severe bleeding, difficulty breathing or swallowing, major facial trauma, "
    "or another medical emergency, please call 911 or go to the nearest emergency room immediately."
)
MEDICAL_DISCLAIMER = (
    "I can share general information, but I can't give medical advice — a dentist needs to examine you "
    "before making any clinical decisions."
)
DEMO_BOOKING_NOTE = "Demo booking — no real appointment will be created."

PRICE_WORDS = re.compile(r"\b(cost|costs|price|prices|pricing|how much|fee|fees|expensive|afford|charge|precio|cuesta|cu[aá]nto)\b", re.I)
COLLECTING_STAGES = {"ask_phone", "ask_service", "ask_patient_type", "ask_contact", "ask_insurance", "ask_member_id", "ask_urgency", "ask_preference", "choose_slot"}
MANAGE_STAGES = {"find_visit", "confirm_cancel"}
STAFF_WAIT_MINUTES = 15
SPANISH_GREETING = (
    "¡Hola! Sí, nuestro equipo habla español. Puedo ayudarle a reservar una cita aquí mismo, "
    "o puede llamar al {phone} y pedir un compañero que hable español. ¿Quiere reservar una cita?"
)
UNKNOWN_HANDOFF = (
    "I want to make sure I give you accurate information, and I don't have a confirmed answer for that. "
    "Our front desk can help at {phone}. I can also answer questions about services, insurance, hours and pricing — or book an appointment."
)

PREFERENCE_REPLIES = ["Earliest available", "This week", "Next week", "Mornings", "Afternoons", "Saturday"]
INSURANCE_REPLIES = ["Delta Dental", "Cigna", "Aetna", "MetLife", "Blue Cross Blue Shield", "No insurance (self-pay)", "Not sure"]
URGENCY_REPLIES = ["As soon as possible", "Within the next 2 weeks", "I'm flexible"]
PATIENT_REPLIES = ["I'm a new patient", "I'm an existing patient", "It's for a family member"]
OFFER_TEXT = {
    "Invisalign": "Would you like to schedule a complimentary Invisalign consultation?",
    "Dental Implants": "Would you like to schedule a complimentary dental implant consultation?",
    "Cosmetic Dentistry": "Would you like to schedule a complimentary cosmetic consultation?",
    "General Dentistry": "Would you like to schedule an exam with one of our dentists?",
    "Emergency Dentistry": "Would you like me to find you an emergency appointment?",
}
START_REPLIES = ["Book an appointment", "Do you accept my insurance?", "Tell me about Invisalign", "I have a dental emergency"]


def _patient_sources(docs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One source chip per topic so hours/insurance FAQs don't also show the clinic-profile duplicate."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for d in docs:
        title = d.get("title")
        if not title:
            continue
        cat = d.get("category") or d.get("id") or title
        if cat in seen:
            continue
        seen.add(cat)
        out.append({"id": d.get("id"), "title": title})
        if len(out) >= 2:
            break
    return out


def _info_followups(top: dict[str, Any] | None, message: str) -> list[str]:
    asked = (message or "").lower()
    title = ((top or {}).get("title") or "").lower()
    cat = (top or {}).get("category") or ""
    if any(w in asked for w in ("hour", "open", "close")) or "hour" in title:
        return ["Book an appointment", "Do you accept my insurance?", "Where are you located?"]
    if any(w in asked for w in ("insurance", "in-network", "ppo")) or cat == "Insurance Question":
        return ["Book an appointment", "What if I don't have insurance?", "What are your hours?"]
    if any(w in asked for w in ("where", "located", "parking", "address")) or "location" in title:
        return ["Book an appointment", "What are your hours?", "Do you accept my insurance?"]
    if any(w in asked for w in ("cost", "price", "how much", "fee")) or cat == "Pricing Question":
        return ["Book an appointment", "Do you accept my insurance?", "Do you offer financing?"]
    return ["Book an appointment", "Do you accept my insurance?", "What are your hours?"]


# ── State & context ──────────────────────────────────────────────────────────
class ReceptionistState(TypedDict, total=False):
    message: str
    action: dict[str, Any] | None
    history: list[dict[str, str]]
    stage: str
    fields: dict[str, Any]
    offered_slots: list[dict[str, Any]]
    intent: str
    selected_slot: str | None
    medical: bool
    red_flags: bool
    ack: list[str]
    retrieved: list[dict[str, Any]]
    reply_parts: list[str]
    quick_replies: list[str]
    ui: dict[str, Any] | None
    slots: list[dict[str, Any]]
    appointment: dict[str, Any] | None
    events: list[dict[str, Any]]
    disclaimer: str | None
    next: str
    trace: list[str]
    topic: str | None
    page: str | None
    spanish: bool
    sources: list[dict[str, str]]


@dataclass
class TurnContext:
    db: Session
    clinic: Clinic
    conversation: Conversation
    services: list[dict[str, Any]]
    docs: list[Doc]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic.timezone)

    @property
    def today(self) -> date:
        return datetime.now(self.tz).date()

    @property
    def assistant(self) -> dict[str, Any]:
        return (self.clinic.settings or {}).get("assistant", {})

    def sync_lead(self, fields: dict[str, Any], create: bool = True) -> Lead | None:
        convo = self.conversation
        lead = self.db.get(Lead, convo.lead_id) if convo.lead_id else None
        if lead is None:
            if not create:
                return None
            lead = Lead(clinic_id=self.clinic.id, source="ai_chat", status="NEW", is_demo=False)
            self.db.add(lead)
            self.db.flush()
            convo.lead_id = lead.id
        for key in ("name", "phone", "email", "service", "patient_type", "insurance", "urgency", "preferred_date", "preferred_time"):
            if key == "name" and fields.get("household_name") and lead.status in {"BOOKED", "CONVERTED"}:
                continue
            if fields.get(key):
                setattr(lead, key, fields[key])
        if fields.get("for_family"):
            extra = fields.get("name")
            household = fields.get("household_name")
            if extra and extra != household:
                note = f"Family booking: {extra}" + (f" for {fields.get('service')}." if fields.get("service") else ".")
                if note not in (lead.notes or ""):
                    lead.notes = ((lead.notes or "") + "\n" + note).strip()
            elif "family member" not in (lead.notes or ""):
                lead.notes = ((lead.notes or "") + "\nBooking for a family member.").strip()
        member_id = fields.get("insurance_member_id")
        if member_id and member_id != "skipped":
            note = f"Member ID: {member_id}"
            if note not in (lead.notes or ""):
                lead.notes = ((lead.notes or "") + "\n" + note).strip()
        lead_svc.refresh_lead(lead)
        return lead


def _ctx(config: RunnableConfig) -> TurnContext:
    return config["configurable"]["ctx"]


def _trace(state: ReceptionistState, node: str) -> list[str]:
    return [*state.get("trace", []), node]


def _first(fields: dict[str, Any]) -> str:
    return (fields.get("name") or "").split(" ")[0]


def _service_phrase(svc: str | None) -> str:
    if not svc:
        return "your visit"
    return svc if svc == "Invisalign" else svc.lower()


def _maybe_recall_patient(ctx: TurnContext, fields: dict[str, Any], ack: list[str], stage: str) -> None:
    if stage not in {"ask_contact", "ask_phone", "ask_patient_type", "followup_capture", "find_visit"}:
        return
    if not (fields.get("phone") or fields.get("email")):
        return
    if fields.get("patient_type") != "existing" and stage != "find_visit":
        return
    mem = memory_svc.load_patient_memory(ctx.db, ctx.clinic, fields, ctx.conversation)
    if mem.upcoming_summary and not any("found your" in a.lower() for a in ack):
        ack.append(f"I found your upcoming visit: {mem.upcoming_summary}.")
        if mem.upcoming_id:
            fields["manage_appointment_id"] = mem.upcoming_id
    elif mem.returning and mem.last_service and not any("been in" in a.lower() for a in ack):
        ack.append(f"Good to have you back — I see you've been in for {mem.last_service}.")


# ── Node: intent detection & entity extraction ───────────────────────────────
def detect_intent(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    msg = state.get("message", "")
    action = state.get("action") or None
    stage = state.get("stage", "start")
    fields = dict(state.get("fields", {}))
    offered = state.get("offered_slots", [])
    ack: list[str] = []
    out: ReceptionistState = {"trace": _trace(state, "intent_detection"), "selected_slot": None, "medical": False, "red_flags": False}

    if action:
        kind = action.get("type")
        if kind == "restart":
            return {**out, "intent": "greeting", "stage": "start", "fields": {}, "offered_slots": [], "ack": []}
        if kind == "select_slot":
            return {**out, "intent": "select_slot", "selected_slot": action.get("slot_id"), "fields": fields, "ack": []}
        if kind == "cancel_appointment":
            fields["manage_mode"] = "cancel"
            if action.get("appointment_id"):
                fields["manage_appointment_id"] = action.get("appointment_id")
            return {**out, "intent": "cancel", "fields": fields, "ack": []}
        if kind == "reschedule_slot":
            fields["manage_mode"] = "reschedule"
            if action.get("appointment_id"):
                fields["manage_appointment_id"] = action.get("appointment_id")
            return {**out, "intent": "select_slot", "selected_slot": action.get("slot_id"), "fields": fields, "ack": []}
        if kind == "submit_contact":
            errors = []
            name = (action.get("name") or "").strip()
            if len(name) >= 2:
                fields["name"] = nlu.title_name(name[:120])
            elif stage == "ask_contact" and not fields.get("name"):
                errors.append("name")
            try:
                phone = normalize_us_phone(action.get("phone") or "") if action.get("phone") else None
                if phone:
                    fields["phone"] = phone
                elif stage == "ask_phone":
                    errors.append("phone")
            except ValueError:
                errors.append("phone")
            email = (action.get("email") or "").strip().lower()
            if email:
                if nlu.EMAIL_RE.fullmatch(email):
                    fields["email"] = email
                else:
                    errors.append("email")
            if errors:
                ack.append("Hmm, a couple of details didn't look quite right (" + ", ".join(errors) + "). Could you double-check them?")
            else:
                _maybe_recall_patient(ctx, fields, ack, stage)
            return {**out, "intent": "details", "fields": fields, "ack": ack}

    ex = nlu.extract(msg, stage, ctx.services, ctx.today, offered)
    intent = nlu.classify(msg, stage, ex)
    if nlu.ENGLISH_SWITCH.search(msg):
        fields["locale"] = "en"
    elif nlu.looks_spanish(msg):
        fields["locale"] = "es"

    page_doc = doc_for_service_slug(ctx.docs, service_slug_from_page(state.get("page")))

    if intent == "unknown" and llm.is_available():
        last_assistant = ""
        for turn in reversed(state.get("history") or []):
            if turn.get("role") == "assistant":
                last_assistant = (turn.get("content") or "")[:240]
                break
        missing = [k for k in ("name", "phone", "email", "service", "insurance") if not fields.get(k)]
        verdict = llm.json_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Classify the dental-clinic website chat message. Respond with JSON {\"intent\": one of "
                        "[\"info\", \"booking\", \"emergency\", \"greeting\", \"thanks\", \"decline\", \"human\", \"cancel\", "
                        "\"reschedule\", \"other\"]}. Use stage and the last assistant question as context. "
                        "Treat closers like 'that's all, thanks' as thanks. Never classify clinical diagnosis requests as booking."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": msg[:500],
                            "stage": stage,
                            "page": state.get("page") or "",
                            "missing_fields": missing,
                            "last_assistant_question": last_assistant,
                        }
                    ),
                },
            ],
            max_tokens=40,
        )
        allowed = {"info", "booking", "emergency", "greeting", "thanks", "decline", "human", "cancel", "reschedule"}
        if verdict and verdict.get("intent") in allowed:
            intent = verdict["intent"]
            out["trace"] = [*out["trace"], "llm_intent_fallback"]

    # Merge extracted entities (don't treat closers/greetings as slot-fills)
    if intent not in {"thanks", "greeting", "human"}:
        if ex.email:
            fields["email"] = ex.email
        if ex.phone:
            fields["phone"] = ex.phone
        if ex.name and (stage in {"ask_contact", "ask_phone", "followup_capture"} or nlu.NAME_PREFIX.search(msg)):
            fields["name"] = ex.name
        if ex.service and (not fields.get("service") or intent in {"booking", "info"} or stage == "ask_service"):
            if fields.get("urgency") != "emergency" or intent == "booking":
                fields["service"] = ex.service
        if intent == "booking" and not fields.get("service") and page_doc and page_doc.service:
            fields["service"] = page_doc.service
        if ex.patient_type:
            fields["patient_type"] = ex.patient_type
        if ex.for_family:
            fields["for_family"] = True
        if ex.insurance:
            fields["insurance"] = ex.insurance
        if stage == "ask_member_id":
            if ex.member_id:
                fields["insurance_member_id"] = ex.member_id
            elif ex.urgency or ex.date_pref or nlu.MEMBER_SKIP.search(msg):
                fields["insurance_member_id"] = fields.get("insurance_member_id") or "skipped"
        elif ex.member_id:
            fields["insurance_member_id"] = ex.member_id
        if ex.urgency and fields.get("urgency") != "emergency":
            fields["urgency"] = ex.urgency
        if ex.date_pref or ex.time_pref:
            if ex.date_pref:
                fields["date_pref"] = ex.date_pref
                fields["preferred_date"] = ex.date_pref["label"]
            if ex.time_pref:
                fields["time_pref"] = ex.time_pref
                fields["preferred_time"] = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Late afternoon", "any": "Any time"}[ex.time_pref]
            if not fields.get("preferred_date"):
                fields["preferred_date"] = "Earliest available"
                fields["date_pref"] = {"label": "Earliest available", "earliest": True}
            if stage == "ask_urgency" and not fields.get("urgency"):
                fields["urgency"] = "soon" if (ex.date_pref or {}).get("earliest") else "normal"

    if stage == "choose_slot":
        if ex.slot_index is not None and offered:
            return {**out, "intent": "select_slot", "selected_slot": offered[ex.slot_index]["id"], "fields": fields, "ack": ack}
        if re.search(r"\b(other|different|more|another|none|later|earlier)\b.*\b(time|times|day|days|option|options|slot|slots)?\b", msg, re.I) and not (ex.date_pref or ex.time_pref):
            fields.pop("date_pref", None)
            fields.pop("preferred_date", None)
            fields.pop("time_pref", None)
            fields.pop("preferred_time", None)
            return {**out, "intent": "details", "fields": fields, "stage": "ask_preference", "ack": ["No problem — let's find a better time."]}

    # Stage-specific acknowledgement snippets
    if intent not in {"thanks", "greeting", "human"}:
        if stage == "ask_service" and ex.service:
            ack.append(f"Great — {ex.service if ex.service == 'Invisalign' else 'a ' + ex.service.lower()} visit.")
        if stage == "ask_patient_type" and ex.patient_type:
            ack.append("Welcome — we love meeting new patients!" if ex.patient_type == "new" else "Great — welcome back!")
        _maybe_recall_patient(ctx, fields, ack, stage)
        if stage == "ask_insurance" and ex.insurance:
            accepted = ctx.clinic.insurance_accepted or []
            if ex.insurance in accepted:
                ack.append(f"Great news — we're in-network with {ex.insurance}.")
            elif ex.insurance == "Self-pay":
                ack.append("No problem — our self-pay new patient special and BrightSmile Membership keep costs simple and transparent.")
            elif ex.insurance == "Not sure":
                ack.append("No problem — our team can verify your coverage before the visit.")
            else:
                ack.append(f"Thanks — we'll verify your {ex.insurance} benefits before your visit. Many PPO plans offer out-of-network coverage.")

    if stage == "ask_phone" and fields.get("phone"):
        ack.append(locale_svc.t(fields, "Thanks — I'll use that number if we get disconnected.", "Gracias — usaré ese número si nos desconectamos."))
        if fields.get("capture_reason") != "booking":
            stage = "offer_booking" if fields.get("service") else "start"
            out["stage"] = stage
            if intent == "details":
                intent = "info"

    if stage == "ask_phone" and intent == "decline":
        fields["phone_declined"] = True
        if fields.get("capture_reason") != "booking":
            intent = "info"
            stage = "start"
            out["stage"] = "start"

    if stage in {"offer_booking", "followup_offer"} and intent == "affirm":
        intent = "booking"
    if stage == "followup_capture" and intent in {"affirm"}:
        intent = "details"
    if stage == "confirm_cancel" and intent == "affirm":
        fields["manage_mode"] = fields.get("manage_mode") or "cancel"
        intent = "cancel"
    if intent in {"cancel", "reschedule", "find_appointment"}:
        fields["manage_mode"] = "cancel" if intent == "cancel" else "reschedule" if intent == "reschedule" else fields.get("manage_mode") or "find"

    if stage == "booked" and (nlu.ANOTHER_FAMILY.search(msg) or (intent == "booking" and nlu.FAMILY.search(msg))):
        fields["household_name"] = fields.get("household_name") or fields.get("name")
        fields["for_family"] = True
        for key in ("service", "name", "patient_type", "date_pref", "time_pref", "preferred_date", "preferred_time", "urgency", "insurance_member_id"):
            fields.pop(key, None)
        intent = "booking"
        ack.append("Happy to book someone else in the family — I'll keep the same phone and email for confirmations.")

    out.update(
        intent=intent,
        fields=fields,
        ack=ack,
        medical=bool(nlu.MEDICAL.search(msg)) and intent in {"info", "unknown"} and not nlu.VISIT_PREP.search(msg),
        red_flags=bool(nlu.RED_FLAGS.search(msg)),
        spanish=fields.get("locale") == "es" or nlu.looks_spanish(msg),
    )
    return out


def route_after_intent(state: ReceptionistState) -> str:
    intent = state.get("intent", "unknown")
    stage = state.get("stage", "start")
    fields = state.get("fields") or {}
    if stage == "ask_phone" and (fields.get("phone") or fields.get("email")):
        return "qualify" if fields.get("capture_reason") == "booking" else "retrieve"
    if intent == "emergency":
        return "emergency"
    if intent == "select_slot":
        return "book"
    if intent in {"cancel", "reschedule", "find_appointment"}:
        return "manage"
    if stage in MANAGE_STAGES and intent in {"details", "affirm", "cancel", "reschedule", "decline"}:
        return "manage"
    if stage == "followup_capture" and intent in {"details", "decline"}:
        return "followup"
    if intent == "decline":
        return "followup"
    if intent in {"booking", "details"}:
        return "qualify"
    return "retrieve"


# ── Node: emergency triage (no diagnosis) ────────────────────────────────────
def emergency(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    fields = dict(state.get("fields", {}))
    fields["urgency"] = "emergency"
    fields["service"] = "Emergency Dentistry"
    fields["preferred_date"] = "Today / next available"
    fields["date_pref"] = {"label": "Today / next available", "earliest": True}
    parts = []
    if state.get("red_flags"):
        parts.append("⚠️ " + EMERGENCY_SAFETY)
    parts.append("I'm sorry you're dealing with that. I can't diagnose what's going on, but I can help you find an emergency appointment — we keep same-day time open for urgent cases.")
    if not state.get("red_flags"):
        parts.append(EMERGENCY_SAFETY)
    hours_note = after_hours_note(ctx.clinic)
    if hours_note:
        parts.append(hours_note)
    return {
        "fields": fields,
        "reply_parts": parts,
        "disclaimer": EMERGENCY_SAFETY,
        "trace": _trace(state, "emergency_triage"),
        "topic": "Emergency Appointment",
    }


# ── Node: qualification (collect lead details) ───────────────────────────────
def _phone_question(fields: dict[str, Any], *, booking: bool, emergency: bool = False) -> tuple[str, str, list[str], dict[str, Any]]:
    fields["capture_reason"] = "booking" if booking else "callback"
    if emergency:
        q = locale_svc.t(
            fields,
            "What's the best mobile number? I'll hold an emergency opening and we can text you the time.",
            "¿Cuál es el mejor celular? Reservamos un espacio de emergencia y le enviamos el horario por mensaje.",
        )
    elif booking:
        q = locale_svc.t(
            fields,
            "What's the best mobile number for this appointment? I'll text the confirmation there.",
            "¿Cuál es el mejor celular para esta cita? Ahí le enviamos la confirmación.",
        )
    else:
        q = locale_svc.t(
            fields,
            "What's the best mobile number in case we get disconnected? I can also text these details.",
            "¿Cuál es el mejor celular por si nos desconectamos? También puedo enviarle estos datos por mensaje.",
        )
    ui = {
        "type": "contact_form",
        "missing": ["phone"],
        "optional": ["name", "email"],
        "phone_first": True,
        "prefill": {k: fields.get(k) for k in ("name", "phone", "email")},
        "submit_label": locale_svc.t(fields, "Save my number", "Guardar mi número"),
        "note": locale_svc.t(
            fields,
            "We'll only use this to reach you about this visit. Demo: a test number is fine.",
            "Solo lo usamos para contactarle por esta visita. Demo: un número de prueba está bien.",
        ),
    }
    skip = [locale_svc.t(fields, "Not right now", "Ahora no")]
    if not booking:
        skip.append(locale_svc.t(fields, "Book an appointment", "Reservar una cita"))
    return "ask_phone", q, skip, ui


def _next_question(ctx: TurnContext, fields: dict[str, Any], prev_stage: str = "start") -> tuple[str, str, list[str], dict[str, Any] | None] | None:
    """Returns (stage, question, quick_replies, ui) for the next missing field, or None when complete."""
    emergency_flow = fields.get("urgency") == "emergency"
    assistant = ctx.assistant
    if not fields.get("phone"):
        return _phone_question(fields, booking=True, emergency=emergency_flow)
    if not fields.get("service"):
        return "ask_service", locale_svc.t(fields, "Which service would you like to book?", "¿Qué servicio le gustaría agendar?"), [s["name"] for s in ctx.services], None
    missing_contact = [f for f in ("name", "phone", "email") if not fields.get(f)]
    if emergency_flow and missing_contact:
        return _contact_question(fields, missing_contact, emergency_flow)
    if not fields.get("patient_type"):
        if fields.get("for_family"):
            clinic_name = ctx.clinic.name
            return (
                "ask_patient_type",
                locale_svc.t(
                    fields,
                    f"Happy to help your family member. Has the patient visited {clinic_name} before?",
                    f"Con gusto ayudo a su familiar. ¿El paciente ya ha visitado {clinic_name}?",
                ),
                locale_svc.patient_replies(fields)[:2] if locale_svc.is_es(fields) else ["New patient", "Existing patient"],
                None,
            )
        svc = fields["service"]
        if emergency_flow or prev_stage == "ask_service":
            lead_in = ""
        else:
            lead_in = locale_svc.t(
                fields,
                f"Absolutely — I can help you get started with {svc if svc == 'Invisalign' else svc.lower()}. ",
                f"Claro — le ayudo con {svc if svc == 'Invisalign' else svc.lower()}. ",
            )
        return (
            "ask_patient_type",
            lead_in
            + locale_svc.t(
                fields,
                f"Is the appointment for yourself, and have you visited {ctx.clinic.name} before?",
                f"¿La cita es para usted, y ha visitado {ctx.clinic.name} antes?",
            ),
            locale_svc.patient_replies(fields),
            None,
        )
    if missing_contact:
        return _contact_question(fields, missing_contact, emergency_flow)
    if assistant.get("collect_insurance", True) and not fields.get("insurance"):
        return (
            "ask_insurance",
            locale_svc.t(fields, "Do you have dental insurance? If so, which provider?", "¿Tiene seguro dental? ¿Cuál es su plan?"),
            locale_svc.insurance_replies(fields),
            None,
        )
    if (
        assistant.get("collect_insurance", True)
        and fields.get("insurance") not in {None, "", "Self-pay", "Not sure"}
        and not fields.get("insurance_member_id")
    ):
        return (
            "ask_member_id",
            locale_svc.t(
                fields,
                "What's the member ID on your card? You can skip this if you don't have it handy — we'll still verify benefits before your visit.",
                "¿Cuál es el número de miembro en su tarjeta? Puede omitirlo si no lo tiene a la mano — igual verificamos los beneficios antes de la cita.",
            ),
            [locale_svc.t(fields, "I'll skip for now", "Lo omito por ahora")],
            None,
        )
    if not emergency_flow and assistant.get("collect_urgency", True) and not fields.get("urgency"):
        return (
            "ask_urgency",
            locale_svc.t(fields, "How soon would you like to come in?", "¿Qué tan pronto le gustaría venir?"),
            locale_svc.urgency_replies(fields),
            None,
        )
    if not emergency_flow and not fields.get("preferred_date"):
        return (
            "ask_preference",
            locale_svc.t(fields, "Do you have a preferred day or time of day?", "¿Tiene un día u horario de preferencia?"),
            locale_svc.preference_replies(fields),
            None,
        )
    return None


def _contact_question(fields: dict[str, Any], missing: list[str], emergency_flow: bool) -> tuple[str, str, list[str], dict[str, Any]]:
    labels = {"name": "full name", "phone": "mobile number", "email": "email address"}
    ui = {"type": "contact_form", "missing": missing, "prefill": {k: fields.get(k) for k in ("name", "phone", "email")},
          "note": "Demo only — please use fictional details, not real patient information."}
    if len(missing) == 3:
        q = (
            locale_svc.t(fields, "To hold an emergency spot, what's the patient's name, mobile number and email?", "Para guardar un espacio de emergencia, ¿cuál es el nombre, celular y correo del paciente?")
            if emergency_flow
            else locale_svc.t(fields, "What's the best name, mobile number and email for the appointment?", "¿Cuál es el nombre, celular y correo para la cita?")
        )
    else:
        pretty = " and ".join(labels[m] for m in missing) if len(missing) <= 2 else ", ".join(labels[m] for m in missing)
        name = _first(fields)
        q = locale_svc.t(
            fields,
            f"Thanks{', ' + name if name else ''}! Could you also share your {pretty}?",
            f"Gracias{', ' + name if name else ''}! ¿Puede compartir también su {pretty}?",
        )
    return "ask_contact", q, [], ui


def qualify(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    fields = dict(state.get("fields", {}))
    parts = list(state.get("reply_parts", []))
    ack = list(state.get("ack", []))
    prev_stage = state.get("stage", "start")
    if fields.get("service") == "Emergency Dentistry" and not fields.get("urgency"):
        fields["urgency"] = "emergency"
        fields["preferred_date"] = "Today / next available"
        fields["date_pref"] = {"label": "Today / next available", "earliest": True}
    if prev_stage == "ask_contact" and fields.get("name") and not ack and all(fields.get(f) for f in ("name", "phone", "email")):
        ack.append(f"Thanks, {_first(fields)}!")
    nxt = _next_question(ctx, fields, prev_stage)
    topic = state.get("topic") or _topic_for(fields)
    if nxt is None:
        return {"fields": fields, "reply_parts": parts + ack, "next": "availability", "trace": _trace(state, "lead_qualification"), "topic": topic}
    stage, question, replies, ui = nxt
    return {
        "fields": fields,
        "stage": stage,
        "reply_parts": parts + ack + [question],
        "quick_replies": replies,
        "ui": ui,
        "next": "end",
        "trace": _trace(state, "lead_qualification"),
        "topic": topic,
    }


def route_after_qualify(state: ReceptionistState) -> str:
    return "availability" if state.get("next") == "availability" else END


def _topic_for(fields: dict[str, Any]) -> str | None:
    svc = fields.get("service")
    if not svc:
        return "Appointment Request"
    if svc == "Emergency Dentistry":
        return "Emergency Appointment"
    if svc == "General Dentistry":
        return "Checkup & Cleaning Appointment"
    return {"Dental Implants": "Dental Implant Consultation", "Invisalign": "Invisalign Consultation", "Cosmetic Dentistry": "Cosmetic Consultation"}.get(svc, f"{svc} Appointment")


# ── Node: appointment availability ───────────────────────────────────────────
def availability(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    fields = state.get("fields", {})
    service = fields.get("service", "General Dentistry")
    dp = fields.get("date_pref") or {}
    tp = fields.get("time_pref")
    pref = scheduler.Preference(
        start_date=date.fromisoformat(dp["start"]) if dp.get("start") else None,
        end_date=date.fromisoformat(dp["end"]) if dp.get("end") else None,
        time_of_day=tp if tp in {"morning", "afternoon", "evening"} else None,
        earliest=bool(dp.get("earliest")) or fields.get("urgency") in {"emergency", "soon"},
    )
    slots = scheduler.get_availability(ctx.db, ctx.clinic, service, pref)
    relaxed = False
    if not slots and pref.time_of_day:
        pref.time_of_day = None
        slots = scheduler.get_availability(ctx.db, ctx.clinic, service, pref)
        relaxed = True
    if not slots:
        slots = scheduler.get_availability(ctx.db, ctx.clinic, service, scheduler.Preference(earliest=True))
        relaxed = True

    parts = list(state.get("reply_parts", []))
    provider = scheduler.provider_for(service)
    if not slots:
        parts.append(f"I'm sorry — I couldn't find open times online right now. Please call us at {ctx.clinic.phone} and we'll fit you in.")
        return {"reply_parts": parts, "stage": "ask_preference", "quick_replies": PREFERENCE_REPLIES, "trace": _trace(state, "appointment_availability")}
    if relaxed:
        parts.append(f"I couldn't find openings matching \"{(fields.get('preferred_date') or '').lower()}{' ' + fields.get('preferred_time', '').lower() if fields.get('preferred_time') else ''}\" — here are the closest available times.")
    elif fields.get("urgency") == "emergency":
        parts.append(locale_svc.t(fields, f"Here are the soonest emergency openings with {provider}:", f"Estos son los horarios de emergencia más próximos con {provider}:"))
    else:
        name = _first(fields)
        thanks = locale_svc.t(fields, f"Thanks, {name}! " if name and not parts else "", f"Gracias, {name}! " if name and not parts else "")
        svc_txt = service if service == "Invisalign" else service.lower()
        parts.append(
            thanks
            + locale_svc.t(
                fields,
                f"Here are the next available {svc_txt} appointments with {provider}:",
                f"Estos son los próximos horarios de {svc_txt} con {provider}:",
            )
        )
    demo = get_settings().demo_mode or scheduler.scheduler_name() == "mock"
    slot_dicts = [s.model_dump() for s in slots]
    return {
        "reply_parts": parts,
        "stage": "choose_slot",
        "offered_slots": slot_dicts,
        "slots": slot_dicts,
        "ui": {"type": "slots", "demo_note": DEMO_BOOKING_NOTE if demo else None},
        "quick_replies": ["Show other times"],
        "trace": _trace(state, "appointment_availability"),
    }


# ── Node: booking ────────────────────────────────────────────────────────────
def book(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    fields = dict(state.get("fields", {}))
    trace = _trace(state, "booking")
    missing = [f for f in ("name", "service") if not fields.get(f)]
    if missing or not (fields.get("phone") or fields.get("email")):
        return {"next": "qualify", "trace": trace}
    slot_id = state.get("selected_slot") or ""
    service = fields["service"]
    try:
        start = scheduler.validate_slot(ctx.db, ctx.clinic, service, slot_id)
    except ValueError as exc:
        return {"next": "availability", "reply_parts": [str(exc)], "trace": trace}

    lead = ctx.sync_lead(fields)
    assert lead is not None
    s = get_settings()
    external_id = None
    sched = scheduler.scheduler_name()
    if sched == "calcom":
        external_id = scheduler.calcom_book(ctx.clinic, start, fields["name"], fields.get("email") or "", fields.get("phone"), service)
        if not external_id:
            sched = "mock"
    appt = Appointment(
        clinic_id=ctx.clinic.id,
        lead_id=lead.id,
        conversation_id=ctx.conversation.id,
        patient_name=fields["name"],
        patient_phone=fields.get("phone"),
        patient_email=fields.get("email"),
        service=service,
        provider_name=scheduler.provider_for(service),
        start_at=start,
        end_at=start + timedelta(minutes=scheduler.duration_for(service)),
        status="CONFIRMED",
        source="ai_chat",
        scheduler=sched,
        external_id=external_id,
        is_demo=False,
        notes=f"Insurance: {fields.get('insurance') or 'n/a'} · Member ID: {fields.get('insurance_member_id') or 'n/a'} · Patient: {fields.get('patient_type') or 'n/a'} · Urgency: {fields.get('urgency') or 'n/a'}",
    )
    ctx.db.add(appt)
    old_id = fields.pop("reschedule_of", None)
    if old_id:
        old = ctx.db.get(Appointment, old_id)
        if old and old.status == "CONFIRMED":
            automations.apply_appointment_status(ctx.db, old, "CANCELLED")
    lead.status = "BOOKED"
    lead_svc.refresh_lead(lead)
    ctx.db.flush()
    ctx.conversation.status = "BOOKED"
    local = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(ctx.tz)
    appointment = {
        "id": appt.id,
        "patient_name": appt.patient_name,
        "service": service,
        "provider": appt.provider_name,
        "date": local.strftime("%A, %B ") + str(local.day),
        "time": scheduler.format_time(local),
        "start": scheduler.iso_z(start),
        "clinic": ctx.clinic.name,
        "address": f"{ctx.clinic.address_line}, {ctx.clinic.city}, {ctx.clinic.state} {ctx.clinic.zip_code}",
        "phone": ctx.clinic.phone,
        "patient_phone": appt.patient_phone,
        "patient_email": appt.patient_email,
        "demo": s.demo_mode or sched == "mock",
        "lead_id": lead.id,
    }
    return {"appointment": appointment, "fields": fields, "next": "confirm", "trace": trace}


def route_after_book(state: ReceptionistState) -> str:
    return state.get("next", "confirm")


# ── Node: confirmation ───────────────────────────────────────────────────────
def confirm(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    appt_info = state["appointment"] or {}
    appt = ctx.db.get(Appointment, appt_info["id"])
    lead = ctx.db.get(Lead, appt_info["lead_id"])
    events = [e.model_dump() for e in automations.send_confirmations(ctx.db, ctx.clinic, lead, appt)]
    first = _first(state.get("fields", {})) or "there"
    svc = appt_info["service"]
    svc_txt = svc if svc == "Invisalign" else svc.lower()
    parts = [
        locale_svc.t(
            state.get("fields"),
            f"You're all set, {first}! ✅ Your {svc_txt} appointment is confirmed for {appt_info['date']} at {appt_info['time']} with {appt_info['provider']}.",
            f"¡Listo, {first}! ✅ Su cita de {svc_txt} está confirmada para el {appt_info['date']} a las {appt_info['time']} con {appt_info['provider']}.",
        )
    ]
    if appt_info.get("demo"):
        if get_settings().email_live:
            parts.append("(Demo booking — no real calendar appointment was created. Confirmation and follow-up emails are sent.)")
        else:
            parts.append("(Demo booking — no real appointment was created and no messages were actually sent.)")
    return {"events": events, "reply_parts": parts, "trace": _trace(state, "confirmation")}


# ── Node: follow-up scheduling after booking ─────────────────────────────────
def schedule_followups(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    appt_info = state["appointment"] or {}
    appt = ctx.db.get(Appointment, appt_info["id"])
    lead = ctx.db.get(Lead, appt_info["lead_id"])
    events = list(state.get("events", [])) + [e.model_dump() for e in automations.schedule_reminders(ctx.db, ctx.clinic, lead, appt)]
    verify = automations.queue_benefits_verification(ctx.db, ctx.clinic, lead, state.get("fields") or {})
    if verify:
        events.append({
            "key": "verify_benefits",
            "label": verify.title,
            "detail": verify.detail or "",
            "status": "pending",
            "channel": "staff",
        })
    parts = list(state.get("reply_parts", []))
    if get_settings().email_live and appt_info.get("patient_email"):
        parts.append("You'll get a confirmation email, plus reminders before your visit. Is there anything else I can help with?")
    else:
        parts.append("You'll get a confirmation by email and text, plus reminders before your visit. Is there anything else I can help with?")
    return {
        "events": events,
        "reply_parts": parts,
        "stage": "booked",
        "offered_slots": [],
        "ui": {"type": "confirmation"},
        "quick_replies": ["What should I bring?", "Book another family member", "I need to reschedule", "Talk to a person", "That's all, thanks!"],
        "trace": _trace(state, "follow_up_scheduling"),
    }


# ── Node: decline → missed-lead follow-up ────────────────────────────────────
def followup(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    fields = dict(state.get("fields", {}))
    stage = state.get("stage", "start")
    intent = state.get("intent")
    trace = _trace(state, "follow_up")
    service = fields.get("service")
    service_txt = (service if service == "Invisalign" else service.lower()) if service else "your visit"

    if stage == "followup_capture" and intent == "decline":
        return {"reply_parts": ["Understood — no problem at all. If you have any other questions, I'm here 24/7."],
                "stage": "start", "quick_replies": START_REPLIES, "trace": trace}

    has_contact = bool(fields.get("phone") or fields.get("email"))
    if has_contact and (fields.get("name") or stage == "followup_capture"):
        if not fields.get("name"):
            fields["name"] = "Website visitor"
        lead = ctx.sync_lead(fields)
        assert lead is not None
        _, event = automations.schedule_missed_lead_followup(ctx.db, ctx.clinic, lead, "Visitor declined booking in chat")
        ctx.conversation.status = "FOLLOW_UP"
        when = event.detail.split(" ", 1)[1] if event and " " in event.detail else "soon"
        first = _first(fields)
        reply = (f"No problem{', ' + first if first and first != 'Website' else ''}! I've noted your interest in {service_txt}. "
                 f"Our team will follow up {when} in case you'd like help finding a time — and you can always book online or call {ctx.clinic.phone}.")
        return {
            "fields": fields,
            "reply_parts": [reply],
            "stage": "followup_scheduled",
            "events": [event.model_dump()] if event else [],
            "ui": {"type": "followup"} if event else None,
            "quick_replies": ["Actually, let's book now"],
            "trace": trace,
        }

    if stage == "followup_capture" and not has_contact:
        _, q, _, ui = _contact_question(fields, [f for f in ("name", "phone", "email") if not fields.get(f)], False)
        return {"fields": fields, "reply_parts": ["I just need a phone number or email so our team can reach you."], "ui": ui, "quick_replies": ["No thanks"], "trace": trace}

    if service:
        return {
            "fields": fields,
            "reply_parts": [f"No problem! If you'd like, leave your name and a phone number or email and our team will send you more information about {service_txt} — no pressure."],
            "stage": "followup_capture",
            "ui": {"type": "contact_form", "missing": ["name", "phone", "email"], "optional": ["email"], "prefill": {},
                   "note": "Demo only — please use fictional details, not real patient information.", "submit_label": "Send me info"},
            "quick_replies": ["No thanks"],
            "trace": trace,
        }
    return {"reply_parts": ["No problem! I'm here if you have any other questions."], "stage": "start", "quick_replies": START_REPLIES, "trace": trace}


def _format_appt(ctx: TurnContext, appt: Appointment) -> str:
    local = appt.start_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(ctx.tz)
    return f"{appt.service} on {local.strftime('%A, %B ')}{local.day} at {scheduler.format_time(local)} with {appt.provider_name or 'our team'}"


def _prep_for_visit(ctx: TurnContext, fields: dict[str, Any], appt: Appointment | None) -> str:
    first = _first(fields)
    ins = fields.get("insurance")
    if ins and ins not in {"Self-pay", "Not sure"}:
        card = f" and your {ins} insurance card"
    elif fields.get("patient_type") == "existing":
        card = " — and your insurance card if anything has changed"
    else:
        card = " and your insurance card"
    lead = f"{first}, " if first else ""
    if appt:
        return (
            f"{lead}for {_format_appt(ctx, appt)}, please bring a photo ID{card}. "
            "If we emailed intake forms, complete those on your phone before you arrive."
        )
    return f"{lead}please bring a photo ID{card}. You'll receive digital intake forms by email before your visit."


def _upcoming_appointments(ctx: TurnContext, fields: dict[str, Any]) -> list[Appointment]:
    now = utcnow()
    rows = list(
        ctx.db.scalars(
            select(Appointment)
            .where(
                Appointment.clinic_id == ctx.clinic.id,
                Appointment.status == "CONFIRMED",
                Appointment.start_at >= now,
            )
            .order_by(Appointment.start_at)
        ).all()
    )
    convo_hits = [a for a in rows if a.conversation_id == ctx.conversation.id]
    if convo_hits:
        return convo_hits
    aid = fields.get("manage_appointment_id")
    if aid:
        hits = [a for a in rows if a.id == aid]
        if hits:
            return hits
    phone = fields.get("phone")
    email = (fields.get("email") or "").lower()
    if phone:
        hits = [a for a in rows if a.patient_phone == phone]
        if hits:
            return hits
    if email:
        hits = [a for a in rows if (a.patient_email or "").lower() == email]
        if hits:
            return hits
    return []


def _cancel_visit(ctx: TurnContext, appt: Appointment) -> list[dict[str, Any]]:
    automations.apply_appointment_status(ctx.db, appt, "CANCELLED")
    events = [
        automations.record(
            ctx.db,
            "appointment_confirmation",
            f"Appointment cancelled for {appt.patient_name}",
            detail=_format_appt(ctx, appt),
            lead_id=appt.lead_id,
            appointment_id=appt.id,
            count_run=False,
        )
    ]
    ctx.db.flush()
    return [e for e in events]


def manage_visit(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    fields = dict(state.get("fields", {}))
    intent = state.get("intent", "unknown")
    stage = state.get("stage", "start")
    mode = fields.get("manage_mode") or ("cancel" if intent == "cancel" else "reschedule" if intent == "reschedule" else "find")
    fields["manage_mode"] = mode
    trace = _trace(state, "manage_visit")

    if stage == "confirm_cancel" and intent == "decline":
        fields.pop("manage_appointment_id", None)
        fields.pop("manage_mode", None)
        return {
            "fields": fields,
            "reply_parts": ["No problem — I'll leave your appointment as it is. Anything else I can help with?"],
            "stage": "booked",
            "quick_replies": ["I need to reschedule", "That's all, thanks!"],
            "trace": trace,
        }

    appts = _upcoming_appointments(ctx, fields)
    if fields.get("manage_appointment_id"):
        appts = [a for a in appts if a.id == fields["manage_appointment_id"]] or appts

    if not appts:
        missing = [f for f in ("phone",) if not fields.get(f)]
        if missing:
            ui = {
                "type": "contact_form",
                "missing": ["phone"],
                "optional": ["name", "email"],
                "prefill": {k: fields.get(k) for k in ("name", "phone", "email")},
                "note": "Demo only — use the phone number from your booking.",
                "submit_label": "Find my appointment",
            }
            return {
                "fields": fields,
                "reply_parts": ["I can look that up. What's the mobile number on the appointment?"],
                "stage": "find_visit",
                "ui": ui,
                "quick_replies": ["Talk to a person"],
                "trace": trace,
            }
        return {
            "fields": fields,
            "reply_parts": [
                f"I couldn't find an upcoming visit with those details. Our front desk can help at {ctx.clinic.phone}, "
                "or I can book a new appointment."
            ],
            "stage": "start",
            "quick_replies": ["Book an appointment", "Talk to a person"],
            "trace": trace,
        }

    if len(appts) > 1 and not fields.get("manage_appointment_id"):
        labels = [_format_appt(ctx, a) for a in appts[:4]]
        fields["offered_appointments"] = [a.id for a in appts[:4]]
        return {
            "fields": fields,
            "reply_parts": ["I found a few upcoming visits. Which one should we update? " + " · ".join(f"{i+1}) {lab}" for i, lab in enumerate(labels))],
            "stage": "find_visit",
            "quick_replies": [f"Appointment {i+1}" for i in range(len(labels))],
            "trace": trace,
        }

    appt = appts[0]
    # "Appointment 2" style pick
    msg = state.get("message") or ""
    offered_ids = fields.get("offered_appointments") or []
    m = re.search(r"\b(?:appointment\s*)?([1-4])\b", msg, re.I)
    if m and offered_ids:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(offered_ids):
            found = ctx.db.get(Appointment, offered_ids[idx])
            if found:
                appt = found
    fields["manage_appointment_id"] = appt.id
    summary = _format_appt(ctx, appt)

    if mode == "find":
        return {
            "fields": fields,
            "reply_parts": [f"You're scheduled for {summary}. Would you like to reschedule or cancel?"],
            "stage": "booked",
            "quick_replies": ["I need to reschedule", "Cancel this appointment", "That's all, thanks!"],
            "trace": trace,
        }

    if mode == "cancel" and stage != "confirm_cancel":
        return {
            "fields": fields,
            "reply_parts": [f"I can cancel {summary}. We ask for 24 hours' notice when possible. Should I cancel it?"],
            "stage": "confirm_cancel",
            "quick_replies": ["Yes, cancel it", "No, keep it", "Reschedule instead"],
            "trace": trace,
        }

    if mode == "cancel":
        _cancel_visit(ctx, appt)
        if ctx.conversation.status == "BOOKED":
            ctx.conversation.status = "FOLLOW_UP"
        fields.pop("manage_appointment_id", None)
        fields.pop("manage_mode", None)
        return {
            "fields": fields,
            "reply_parts": [
                f"Done — I cancelled {summary}. If you'd like a new time I can book one now, or call {ctx.clinic.phone}."
            ],
            "stage": "start",
            "quick_replies": ["Book a new appointment", "That's all, thanks!"],
            "trace": [*trace, "appointment_cancelled"],
        }

    # reschedule
    fields["reschedule_of"] = appt.id
    fields["service"] = appt.service
    fields["name"] = fields.get("name") or appt.patient_name
    fields["phone"] = fields.get("phone") or appt.patient_phone
    fields["email"] = fields.get("email") or appt.patient_email
    fields["preferred_date"] = "Earliest available"
    fields["date_pref"] = {"label": "Earliest available", "earliest": True}
    return {
        "fields": fields,
        "reply_parts": [f"Of course. I'll find a new time for your {appt.service} visit (currently {summary})."],
        "next": "availability",
        "trace": [*trace, "appointment_reschedule"],
    }


def route_after_manage(state: ReceptionistState) -> str:
    return "availability" if state.get("next") == "availability" else END


# ── Nodes: knowledge retrieval + grounded response ───────────────────────────
def _user_questions(history: list[dict[str, str]] | None, *, include_current: bool = False, current: str | None = None) -> list[str]:
    users = [((m.get("content") or "").strip()) for m in (history or []) if m.get("role") == "user"]
    users = [q for q in users if q]
    if include_current:
        return users
    cur = (current or "").strip()
    if cur and users and users[-1] == cur:
        users = users[:-1]
    elif not cur and users:
        users = users[:-1]
    return users


def _followup_query(state: ReceptionistState) -> str:
    msg = (state.get("message") or "").strip()
    prior = _user_questions(state.get("history"), current=msg)
    last = prior[-1] if prior else ""
    if nlu.ALL_PRICES.search(msg) or nlu.MEMORY_Q.search(msg):
        return f"{' '.join(prior[-3:])} {msg}".strip()
    if last and nlu.ANAPHORA.search(msg):
        return f"{last} {msg}".strip()
    return msg


def retrieve_knowledge(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    intent = state.get("intent", "unknown")
    query = _followup_query(state)
    page_doc = doc_for_service_slug(ctx.docs, service_slug_from_page(state.get("page")))
    if page_doc and page_doc.service and intent in {"info", "unknown", "greeting"} and not nlu.ALL_PRICES.search(state.get("message") or ""):
        query = f"{page_doc.service} {query}".strip()
    hits: list[tuple[Doc, float]] = retrieve(query, ctx.docs) if intent in {"info", "unknown"} else []
    if nlu.ALL_PRICES.search(state.get("message") or "") or nlu.ALL_PRICES.search(query):
        catalog = service_price_catalog(ctx.docs)
        if catalog:
            hits = [(catalog, 120.0)] + [(d, sc) for d, sc in hits if d.id != catalog.id]
    if page_doc and intent in {"greeting", "info", "unknown"} and not nlu.ALL_PRICES.search(state.get("message") or ""):
        hits = [(page_doc, 99.0)] + [(d, sc) for d, sc in hits if d.id != page_doc.id]
    return {
        "retrieved": [{"id": d.id, "title": d.title, "answer": d.answer, "category": d.category, "service": d.service, "score": round(sc, 2), "meta": d.meta} for d, sc in hits[:5]],
        "trace": _trace(state, "knowledge_retrieval"),
    }


def _facts_blob(docs: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {d['title']}: {d['answer']}" for d in docs)


def _ungrounded(answer: str, facts: str, clinic: Clinic) -> bool:
    compact_facts = facts.replace(" ", "")
    for amount in re.findall(r"\$\s?[\d,]+", answer):
        if amount.replace(" ", "") not in compact_facts:
            return True
    clinic_digits = re.sub(r"\D", "", clinic.phone or "")
    facts_digits = re.sub(r"\D", "", facts)
    for match in nlu.PHONE_RE.finditer(answer):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if digits and digits not in facts_digits and digits != clinic_digits[-10:]:
            return True
    for name in clinic.insurance_accepted or []:
        if name.lower() in answer.lower() and name.lower() not in facts.lower():
            return True
    return False


def _llm_answer(ctx: TurnContext, state: ReceptionistState, docs: list[dict[str, Any]]) -> str | None:
    facts = _facts_blob(docs)
    history = "\n".join(f"{m['role']}: {m['content']}" for m in state.get("history", [])[-12:])
    if not facts and not history:
        return None
    name = ctx.assistant.get("name", "Ava")
    working = memory_svc.working_memory_lines(
        state.get("fields") or {},
        state.get("stage") or "start",
        memory_svc.load_patient_memory(ctx.db, ctx.clinic, state.get("fields") or {}, ctx.conversation),
    )
    system = (
        f"You are {name}, the AI receptionist for {ctx.clinic.name}, a dental practice in {ctx.clinic.city}, {ctx.clinic.state}. "
        "Use CLINIC FACTS for clinic details (prices, hours, insurance, services). "
        "Use RECENT CONVERSATION to remember what the patient already asked and to answer follow-ups like "
        "'those', 'each of them', or 'do you remember what I asked?'. "
        "If they ask you to recap, list their previous questions in plain language and offer to go deeper. "
        "If the facts don't contain a clinic detail, say you're not certain and offer "
        f"to connect them with the front desk at {ctx.clinic.phone}. Never diagnose conditions, never recommend medications or a specific "
        "treatment for the patient — for clinical questions say a dentist needs to evaluate them. Do not invent prices, insurers, "
        "services, staff, or hours. Do not ask for personal details. Reply in 2–4 short, warm, professional sentences. No markdown."
    )
    if working:
        system += " WORKING MEMORY for this visit (do not contradict; do not add new clinical facts): " + "; ".join(working) + "."
    page_slug = service_slug_from_page(state.get("page"))
    if page_slug:
        system += f" The visitor is currently on the /services/{page_slug} page — prefer that service when the question is ambiguous."
    if state.get("spanish"):
        system += " The patient wrote in Spanish — reply in Spanish, still using only the clinic facts and conversation."
    user = f"CLINIC FACTS:\n{facts or '(none)'}\n\nRECENT CONVERSATION:\n{history or '(none)'}\n\nPATIENT MESSAGE: {state.get('message', '')}"
    answer = llm.chat_completion([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=260)
    if not answer:
        return None
    if _ungrounded(answer, facts or "", ctx.clinic):
        return None
    return answer


def respond(state: ReceptionistState, config: RunnableConfig) -> ReceptionistState:
    ctx = _ctx(config)
    intent = state.get("intent", "unknown")
    stage = state.get("stage", "start")
    fields = dict(state.get("fields", {}))
    docs = state.get("retrieved", [])
    parts: list[str] = []
    quick: list[str] = []
    ui = None
    topic = state.get("topic")
    new_stage = stage
    disclaimer = None
    trace = _trace(state, "response")
    sources: list[dict[str, str]] = []
    events: list[dict[str, Any]] = []
    page_doc = next((d for d in docs if d.get("meta", {}).get("slug") and d.get("score", 0) >= 90), None)

    if intent == "greeting":
        mem = memory_svc.load_patient_memory(ctx.db, ctx.clinic, fields, ctx.conversation)
        first = mem.first_name or _first(fields)
        upcoming = _upcoming_appointments(ctx, fields)
        if stage in COLLECTING_STAGES and fields.get("service"):
            nxt = _next_question(ctx, fields, stage)
            parts.append(
                f"Welcome back{', ' + first if first else ''} — we were booking {_service_phrase(fields.get('service'))}."
            )
            if nxt:
                new_stage, q, quick, ui = nxt
                if q.startswith("Absolutely"):
                    q = q.split(". ", 1)[-1]
                parts.append(q)
        elif upcoming:
            appt = upcoming[0]
            parts.append(
                f"Welcome back{', ' + first if first else ''}! You're still set for {_format_appt(ctx, appt)}. "
                "Need to change it, or is there something else I can help with?"
            )
            quick = ["I need to reschedule", "Cancel this appointment", "That's all, thanks!"]
            new_stage = "booked"
        elif first and (fields.get("service") or stage == "booked"):
            parts.append(f"Welcome back, {first}! How can I help today?")
            hours_note = after_hours_note(ctx.clinic)
            if hours_note:
                parts.append(hours_note)
            quick = locale_svc.start_replies(fields)
            new_stage = "start" if stage not in COLLECTING_STAGES else stage
        else:
            if state.get("spanish"):
                parts.append(locale_svc.spanish_greeting(ctx.clinic.phone))
            else:
                parts.append(ctx.assistant.get("greeting") or f"Hi! I'm the AI receptionist for {ctx.clinic.name}. How can I help?")
            hours_note = after_hours_note(ctx.clinic)
            if hours_note:
                parts.append(hours_note)
            if page_doc:
                svc = page_doc.get("service")
                snippet = (page_doc.get("answer") or "").split(".")[0]
                parts.append(
                    locale_svc.t(fields, f"I see you're looking at {svc}. {snippet}.", f"Veo que está viendo {svc}. {snippet}.")
                )
                sources.append({"id": page_doc["id"], "title": page_doc["title"]})
                if svc:
                    fields.setdefault("service", svc)
                    parts.append(locale_svc.offer_text(fields, svc, OFFER_TEXT))
                    quick = [locale_svc.t(fields, "Yes, let's book", "Sí, reservemos"), locale_svc.t(fields, "Not right now", "Ahora no")]
                    new_stage = "offer_booking"
            if not quick:
                quick = locale_svc.start_replies(fields)
            new_stage = new_stage if new_stage == "offer_booking" else ("start" if stage not in COLLECTING_STAGES else stage)
    elif intent == "thanks":
        if stage == "booked":
            parts.append("You're very welcome! We look forward to seeing you. 😊")
            quick = ["I need to reschedule", "What should I bring?", "Talk to a person"]
        elif stage in COLLECTING_STAGES and (fields.get("phone") or fields.get("email")) and fields.get("service"):
            lead = ctx.sync_lead(fields)
            assert lead is not None
            _, event = automations.schedule_missed_lead_followup(
                ctx.db, ctx.clinic, lead, "Visitor paused an unfinished booking"
            )
            first = _first(fields)
            when = event.detail.split(" ", 1)[1] if event and " " in event.detail else "soon"
            parts.append(
                f"You're welcome{', ' + first if first else ''}! I'll follow up {when} in case you'd like to finish "
                f"booking {_service_phrase(fields.get('service'))} — or you can pick it back up here anytime."
            )
            if event:
                events = [event.model_dump()]
                ctx.conversation.status = "FOLLOW_UP"
                ui = {"type": "followup"}
            new_stage = "followup_scheduled"
            quick = ["Actually, let's book now", "Talk to a person"]
        else:
            parts.append("You're welcome! Is there anything else I can help you with?")
            quick = START_REPLIES
            if stage in COLLECTING_STAGES:
                new_stage = "start"
    elif intent == "human":
        ctx.conversation.status = "WAITING_STAFF"
        fields["waiting_since"] = utcnow().isoformat()
        lead = ctx.db.get(Lead, ctx.conversation.lead_id) if ctx.conversation.lead_id else None
        alert = automations.alert_staff_handoff(ctx.db, ctx.clinic, ctx.conversation, lead)
        if alert:
            events.append(alert.model_dump())
        parts.append(
            f"Of course — I've flagged this chat for our front desk. A team member will join here shortly. "
            f"If it's urgent you can also call {ctx.clinic.phone}. You're welcome to keep typing; they'll see it."
        )
        quick = ["I need to reschedule", "That's all, thanks!"] if stage == "booked" else ["Book an appointment", "I'll wait"]
        new_stage = "waiting_staff"
        trace = [*trace, "staff_handoff"]
    else:
        msg = state.get("message") or ""
        upcoming = _upcoming_appointments(ctx, fields) if stage == "booked" or nlu.VISIT_PREP.search(msg) else []
        if nlu.VISIT_PREP.search(msg) and (stage == "booked" or upcoming):
            parts.append(_prep_for_visit(ctx, fields, upcoming[0] if upcoming else None))
            if stage == "booked" or upcoming:
                quick = ["I need to reschedule", "Talk to a person", "That's all, thanks!"]
                new_stage = "booked"
        elif nlu.MEMORY_Q.search(msg):
            asked = [q for q in _user_questions(state.get("history"), current=msg) if not nlu.MEMORY_Q.search(q)]
            if asked:
                listed = "; ".join(asked[-5:])
                parts.append(
                    locale_svc.t(
                        fields,
                        f"Yes — I have this conversation. So far you've asked: {listed}. I can go deeper on any of those, or help you book.",
                        f"Sí — tengo esta conversación. Hasta ahora preguntó: {listed}. Puedo profundizar en cualquiera o ayudarle a reservar.",
                    )
                )
                quick = ["Book an appointment", asked[-1][:80] if asked else "What are your hours?", "Do you accept my insurance?"]
                trace = [*trace, "conversation_memory"]
            else:
                parts.append(
                    locale_svc.t(
                        fields,
                        "This is the start of our chat, so I don't have an earlier question to recap yet. What would you like to know?",
                        "Este es el inicio del chat, así que aún no hay una pregunta anterior. ¿En qué le ayudo?",
                    )
                )
                quick = locale_svc.start_replies(fields)
            if asked:
                fields["last_questions"] = asked[-8:]
        else:
            if state.get("medical"):
                disclaimer = MEDICAL_DISCLAIMER
            top = docs[0] if docs else None
            answer = None
            if llm.is_available() and (top or len(state.get("history") or []) > 2):
                answer = _llm_answer(ctx, state, docs)
                if answer:
                    trace = [*trace, "llm_grounded_answer"]
            if not answer and top:
                answer = top["answer"]
                if top["category"] == "Insurance Question":
                    answer = insurance_specific_answer(state.get("message", ""), ctx.clinic.insurance_accepted or []) or answer
                if top.get("service") and PRICE_WORDS.search(state.get("message", "")) and top["meta"].get("price_note"):
                    answer += " " + top["meta"]["price_note"]
                elif len(docs) > 1 and docs[1]["score"] >= 2 and docs[1]["category"] != top["category"] and docs[1]["score"] >= top["score"] * 0.75:
                    answer += " " + docs[1]["answer"]
            if state.get("medical"):
                if top and top.get("service"):
                    parts.append(MEDICAL_DISCLAIMER + " " + (answer or ""))
                else:
                    parts.append(MEDICAL_DISCLAIMER + " The best next step is an exam so one of our dentists can take a look. " +
                                 (answer if answer and top and top["id"].startswith("faq") and top["score"] >= 2 else ""))
                    fields.setdefault("service", "General Dentistry")
            elif answer:
                parts.append(answer)
            else:
                parts.append(UNKNOWN_HANDOFF.format(phone=ctx.clinic.phone))
                quick = START_REPLIES

            if top:
                topic = topic or top["category"]
                if top.get("service"):
                    topic = _topic_for({"service": top["service"]})
                sources = _patient_sources(docs)

            # Resume an in-progress booking, or offer one when interest is shown.
            if stage in COLLECTING_STAGES:
                nxt = _next_question(ctx, fields, stage)
                if nxt:
                    new_stage, q, quick, ui = nxt
                    if q.startswith("Absolutely"):
                        q = q.split(". ", 1)[-1]
                    if new_stage == "ask_phone" and fields.get("capture_reason") != "booking":
                        parts.append(q)
                    else:
                        parts.append("Now, back to your appointment — " + q[0].lower() + q[1:])
                elif stage == "choose_slot":
                    parts.append("Whenever you're ready, just pick one of the times above.")
                    quick = ["Show other times"]
            elif stage in {"booked", "followup_scheduled", "waiting_staff"}:
                if stage == "booked" and not quick:
                    quick = ["I need to reschedule", "What should I bring?", "Book another family member", "Talk to a person"]
            elif top and (top.get("service") or top["id"] in {"faq:booking"} or "book" in top["title"].lower() or state.get("medical")):
                svc = top.get("service") or fields.get("service")
                if svc and not fields.get("service"):
                    fields["service"] = svc
                if svc:
                    parts.append(OFFER_TEXT.get(svc, f"Would you like to schedule a {svc.lower()} appointment?") if not locale_svc.is_es(fields) else locale_svc.offer_text(fields, svc, OFFER_TEXT))
                else:
                    parts.append(locale_svc.t(fields, "Would you like me to book an appointment for you now?", "¿Quiere que le agende una cita ahora?"))
                quick = [locale_svc.t(fields, "Yes, let's book", "Sí, reservemos"), locale_svc.t(fields, "Not right now", "Ahora no")]
                new_stage = "offer_booking"
            elif not quick and stage not in {"booked", "followup_scheduled", "waiting_staff"}:
                quick = _info_followups(top, state.get("message") or "")

    if (
        intent not in {"greeting", "thanks", "human"}
        and new_stage not in {"booked", "waiting_staff", "followup_scheduled", "choose_slot", "ask_contact", "ask_patient_type", "ask_insurance", "ask_member_id", "ask_urgency", "ask_preference", "ask_service"}
        and not (ui and ui.get("type") == "contact_form")
        and not fields.get("phone")
        and not fields.get("email")
        and not fields.get("phone_declined")
    ):
        booking = new_stage == "offer_booking" or (bool(fields.get("service")) and new_stage in {"offer_booking", "ask_phone"})
        st, q, qr, form = _phone_question(
            fields,
            booking=booking or new_stage == "offer_booking",
            emergency=fields.get("urgency") == "emergency",
        )
        parts.append(q)
        quick = qr
        ui = form
        new_stage = st

    return {
        "reply_parts": list(state.get("reply_parts", [])) + list(state.get("ack", [])) + parts,
        "quick_replies": quick,
        "ui": ui,
        "stage": new_stage,
        "fields": fields,
        "disclaimer": disclaimer or state.get("disclaimer"),
        "topic": topic,
        "sources": sources,
        "events": events,
        "trace": trace,
    }


# ── Graph assembly ───────────────────────────────────────────────────────────
def build_graph():
    g = StateGraph(ReceptionistState)
    g.add_node("detect_intent", detect_intent)
    g.add_node("emergency", emergency)
    g.add_node("retrieve", retrieve_knowledge)
    g.add_node("respond", respond)
    g.add_node("qualify", qualify)
    g.add_node("availability", availability)
    g.add_node("book", book)
    g.add_node("confirm", confirm)
    g.add_node("schedule_followups", schedule_followups)
    g.add_node("followup", followup)
    g.add_node("manage", manage_visit)

    g.add_edge(START, "detect_intent")
    g.add_conditional_edges("detect_intent", route_after_intent,
                            {"emergency": "emergency", "book": "book", "followup": "followup", "qualify": "qualify",
                             "retrieve": "retrieve", "manage": "manage"})
    g.add_edge("emergency", "qualify")
    g.add_edge("retrieve", "respond")
    g.add_edge("respond", END)
    g.add_conditional_edges("qualify", route_after_qualify, {"availability": "availability", END: END})
    g.add_edge("availability", END)
    g.add_conditional_edges("book", route_after_book, {"confirm": "confirm", "availability": "availability", "qualify": "qualify"})
    g.add_edge("confirm", "schedule_followups")
    g.add_edge("schedule_followups", END)
    g.add_edge("followup", END)
    g.add_conditional_edges("manage", route_after_manage, {"availability": "availability", END: END})
    return g.compile()


receptionist_graph = build_graph()

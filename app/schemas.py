"""Pydantic request/response schemas (input validation lives here)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer, field_validator, model_validator

from .models import LEAD_STATUSES

# Naive UTC datetimes are serialized with an explicit Z so browsers parse them correctly.
UTCDateTime = Annotated[
    datetime,
    PlainSerializer(lambda d: d.replace(microsecond=0).isoformat() + "Z", return_type=str),
]

PHONE_DIGITS = re.compile(r"\D+")


def normalize_us_phone(value: str | None) -> str | None:
    if value is None:
        return None
    digits = PHONE_DIGITS.sub("", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Please enter a valid 10-digit US phone number")
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def clean_text(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()
    return value[:max_len] if value else None


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Chat ─────────────────────────────────────────────────────────────────────
class ChatAction(BaseModel):
    type: Literal["select_slot", "submit_contact", "restart", "cancel_appointment", "reschedule_slot"]
    slot_id: str | None = Field(default=None, max_length=64)
    appointment_id: int | None = None
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=160)


class ChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    message: str = Field(default="", max_length=1000)
    action: ChatAction | None = None
    page: str | None = Field(default=None, max_length=200)
    # A prospect's private demo link (/d/<token>): the assistant answers as that clinic.
    demo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,40}$")

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        return clean_text(v, 1000) or ""

    @model_validator(mode="after")
    def require_content(self) -> ChatRequest:
        if not self.message and not self.action:
            raise ValueError("Either a message or an action is required")
        return self


class Slot(BaseModel):
    id: str
    start: str
    end: str
    date_label: str
    time_label: str
    label: str
    provider: str | None = None


class FlowEvent(BaseModel):
    key: str
    label: str
    detail: str = ""
    status: Literal["completed", "simulated", "scheduled", "failed", "pending"] = "completed"
    channel: str | None = None
    at: str | None = None


class LeadSummary(BaseModel):
    id: int | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    service: str | None = None
    patient_type: str | None = None
    insurance: str | None = None
    urgency: str | None = None
    preferred_date: str | None = None
    preferred_time: str | None = None
    status: str = "NEW"
    score_label: str = "COLD"


class ChatSource(BaseModel):
    id: str | None = None
    title: str


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    quick_replies: list[str] = []
    ui: dict[str, Any] | None = None
    slots: list[Slot] = []
    appointment: dict[str, Any] | None = None
    events: list[FlowEvent] = []
    lead: LeadSummary | None = None
    stage: str
    intent: str
    disclaimer: str | None = None
    demo_mode: bool
    ai_provider: str
    sources: list[ChatSource] = []
    waiting_staff: bool = False
    staff_brief: str | None = None
    last_message_id: int | None = None
    new_session: bool = False


# ── Leads ────────────────────────────────────────────────────────────────────
LeadStatus = Literal["NEW", "CONTACTED", "QUALIFIED", "BOOKED", "FOLLOW_UP", "CONVERTED", "LOST"]
assert set(LeadStatus.__args__) == set(LEAD_STATUSES)  # type: ignore[attr-defined]


class LeadCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    service: str | None = Field(default=None, max_length=120)
    patient_type: Literal["new", "existing"] | None = None
    insurance: str | None = Field(default=None, max_length=120)
    urgency: Literal["emergency", "soon", "normal", "flexible"] | None = None
    preferred_date: str | None = Field(default=None, max_length=80)
    preferred_time: str | None = Field(default=None, max_length=40)
    message: str | None = Field(default=None, max_length=2000)
    source: Literal["contact_form", "ai_chat", "booking_form", "phone", "manual"] = "contact_form"

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalize_us_phone(v) if v else None

    @field_validator("name", "service", "insurance", "message")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v, 2000)

    @model_validator(mode="after")
    def need_contact(self) -> LeadCreate:
        if not self.phone and not self.email:
            raise ValueError("A phone number or email is required")
        return self


class LeadUpdate(BaseModel):
    status: LeadStatus | None = None
    notes: str | None = Field(default=None, max_length=4000)


class LeadOut(ORM):
    id: int
    name: str | None
    phone: str | None
    email: str | None
    service: str | None
    patient_type: str | None
    insurance: str | None
    urgency: str | None
    preferred_date: str | None
    preferred_time: str | None
    status: str
    score: int
    score_label: str
    source: str
    notes: str
    is_demo: bool
    created_at: UTCDateTime
    last_activity_at: UTCDateTime


class MessageOut(ORM):
    id: int
    role: str
    content: str
    meta: dict[str, Any]
    created_at: UTCDateTime


class ConversationOut(ORM):
    id: str
    lead_id: int | None
    channel: str
    intent: str
    service: str | None
    stage: str
    status: str
    lead_score: str
    is_demo: bool
    created_at: UTCDateTime
    last_message_at: UTCDateTime
    patient_name: str | None = None
    message_count: int = 0
    preview: str | None = None
    staff_brief: str | None = None
    page: str | None = None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


class StaffReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def _clean_msg(cls, v: str) -> str:
        return clean_text(v, 2000) or ""


class AppointmentOut(ORM):
    id: int
    lead_id: int | None
    patient_name: str
    patient_phone: str | None
    patient_email: str | None
    service: str
    provider_name: str | None
    start_at: UTCDateTime
    end_at: UTCDateTime
    status: str
    source: str
    scheduler: str
    is_demo: bool
    notes: str
    created_at: UTCDateTime


class FollowUpOut(ORM):
    id: int
    lead_id: int
    appointment_id: int | None
    kind: str
    channel: str
    recipient: str
    subject: str | None
    message: str
    due_at: UTCDateTime
    status: str
    sent_at: UTCDateTime | None
    created_at: UTCDateTime
    lead_name: str | None = None


class AutomationEventOut(ORM):
    id: int
    automation_key: str
    lead_id: int | None
    appointment_id: int | None
    title: str
    detail: str
    channel: str | None
    status: str
    created_at: UTCDateTime


class LeadDetail(LeadOut):
    conversations: list[ConversationDetail] = []
    appointments: list[AppointmentOut] = []
    followups: list[FollowUpOut] = []
    events: list[AutomationEventOut] = []


# ── Appointments ─────────────────────────────────────────────────────────────
class AppointmentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(max_length=40)
    email: EmailStr
    service: str = Field(min_length=2, max_length=120)
    slot_id: str = Field(max_length=64, description="Slot start time (ISO 8601, UTC) from /api/appointments/availability")
    patient_type: Literal["new", "existing"] = "new"
    insurance: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)
    source: Literal["booking_form", "ai_chat", "phone", "manual"] = "booking_form"

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_us_phone(v)  # type: ignore[return-value]

    @field_validator("name", "service", "insurance", "notes")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v, 1000)


class AppointmentUpdate(BaseModel):
    status: Literal["CONFIRMED", "COMPLETED", "CANCELLED", "NO_SHOW"]


class BookingResult(BaseModel):
    appointment: AppointmentOut
    lead_id: int
    events: list[FlowEvent]
    demo: bool


class AvailabilityOut(BaseModel):
    service: str
    timezone: str
    scheduler: str
    demo: bool
    slots: list[Slot]


# ── Follow-ups ───────────────────────────────────────────────────────────────
class FollowUpCreate(BaseModel):
    lead_id: int
    kind: Literal["missed_lead", "reminder_24h", "reminder_2h", "review_request", "treatment_plan"] = "missed_lead"
    channel: Literal["sms", "email"] | None = None
    delay_hours: float = Field(default=24, ge=0, le=24 * 30)
    message: str | None = Field(default=None, max_length=1000)


# ── Automations ──────────────────────────────────────────────────────────────
class AutomationOut(ORM):
    id: int
    key: str
    name: str
    description: str
    trigger: str
    steps: list[dict[str, Any]]
    status: str
    config: dict[str, Any]
    runs_count: int
    last_run_at: UTCDateTime | None


class AutomationUpsert(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{3,60}$")
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    trigger: str | None = Field(default=None, max_length=160)
    steps: list[dict[str, Any]] | None = None
    status: Literal["ACTIVE", "PAUSED"] | None = None
    config: dict[str, Any] | None = None


# ── Clinic ───────────────────────────────────────────────────────────────────
class ServiceOut(ORM):
    id: int
    slug: str
    name: str
    category: str
    short_description: str
    description: str
    duration_minutes: int
    price_from: int | None
    price_note: str
    is_active: bool


class FAQOut(ORM):
    id: int
    question: str
    answer: str
    category: str
    keywords: list[str] = []
    sort_order: int = 0


class ClinicOut(ORM):
    id: int
    name: str
    tagline: str
    phone: str
    email: str
    address_line: str
    city: str
    state: str
    zip_code: str
    timezone: str
    hours: dict[str, Any]
    insurance_accepted: list[str]
    financing: dict[str, Any]
    settings: dict[str, Any]
    services: list[ServiceOut] = []
    faqs: list[FAQOut] = []


class ServicePatch(BaseModel):
    id: int
    is_active: bool | None = None
    duration_minutes: int | None = Field(default=None, ge=10, le=240)
    price_note: str | None = Field(default=None, max_length=300)
    short_description: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=4000)


class ClinicUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    tagline: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    address_line: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, max_length=40)
    zip_code: str | None = Field(default=None, max_length=20)
    timezone: str | None = Field(default=None, max_length=60)
    hours: dict[str, Any] | None = None
    insurance_accepted: list[Annotated[str, Field(max_length=80)]] | None = None
    financing: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    services: list[ServicePatch] | None = None

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        time_re = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
        for day, span in v.items():
            if day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
                raise ValueError(f"Unknown day '{day}'")
            if span is None:
                continue
            if not (isinstance(span, dict) and time_re.match(str(span.get("open", ""))) and time_re.match(str(span.get("close", "")))):
                raise ValueError(f"Invalid hours for {day}; use HH:MM")
            if span["open"] >= span["close"]:
                raise ValueError(f"Opening time must be before closing time on {day}")
        return v

    @field_validator("financing")
    @classmethod
    def validate_financing(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        allowed = {"summary", "membership_plan", "providers"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"Unknown financing fields: {', '.join(sorted(unknown))}")
        out: dict[str, Any] = {}
        if "summary" in v:
            out["summary"] = str(v.get("summary") or "")[:500]
        if "membership_plan" in v:
            out["membership_plan"] = str(v.get("membership_plan") or "")[:400]
        if "providers" in v:
            raw = v.get("providers") or []
            if isinstance(raw, str):
                raw = [p.strip() for p in raw.split(",") if p.strip()]
            if not isinstance(raw, list):
                raise ValueError("financing.providers must be a list")
            out["providers"] = [str(p).strip()[:80] for p in raw if str(p).strip()][:12]
        return out


class IntegrationField(BaseModel):
    key: str
    label: str
    secret: bool
    configured: bool
    hint: str | None = None
    value: str | None = None
    placeholder: str = ""


class IntegrationStatus(BaseModel):
    provider: str
    name: str
    category: str
    status: Literal["connected", "not_connected", "demo"]
    detail: str
    env_vars: list[str]
    fields: list[IntegrationField] = []
    configured_in: Literal["env", "admin", "both", "none"] = "none"


class IntegrationUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def limit_values(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 12:
            raise ValueError("Too many fields")
        cleaned: dict[str, Any] = {}
        for key, val in v.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,60}", str(key)):
                raise ValueError(f"Invalid setting key '{key}'")
            if val is None or isinstance(val, bool):
                cleaned[str(key)] = val
                continue
            text = str(val).strip()
            if len(text) > 500:
                raise ValueError(f"{key} is too long")
            cleaned[str(key)] = text
        return cleaned


class RuntimeUpdate(BaseModel):
    demo_mode: bool | None = None


class FAQWrite(BaseModel):
    question: str = Field(min_length=5, max_length=300)
    answer: str = Field(min_length=5, max_length=4000)
    category: str = Field(default="general", max_length=60)
    keywords: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=24)


class KnowledgePasteIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    text: str = Field(min_length=20, max_length=80_000)
    keywords: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=24)


class KnowledgeDocOut(ORM):
    id: int
    title: str
    filename: str | None = None
    source: str
    char_count: int
    is_active: bool
    created_at: UTCDateTime
    preview: str = ""


class WaitlistCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    service: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalize_us_phone(v) if v else None

    @field_validator("name", "service", "notes")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v, 1000)

    @model_validator(mode="after")
    def need_contact(self) -> WaitlistCreate:
        if not self.phone and not self.email:
            raise ValueError("A phone number or email is required")
        return self


class WaitlistUpdate(BaseModel):
    status: Literal["WAITING", "NOTIFIED", "BOOKED", "CANCELLED"] | None = None
    notes: str | None = Field(default=None, max_length=1000)


class WaitlistOut(ORM):
    id: int
    name: str
    phone: str | None
    email: str | None
    service: str | None
    notes: str
    status: str
    created_at: UTCDateTime


class IntakePreview(BaseModel):
    appointment_id: int
    clinic_name: str
    patient_first_name: str
    service: str
    date_label: str
    time_label: str
    already_submitted: bool = False


class IntakeSubmit(BaseModel):
    appointment_id: int
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    reason: str | None = Field(default=None, max_length=500)
    medications: str | None = Field(default=None, max_length=500)
    allergies: str | None = Field(default=None, max_length=500)
    insurance: str | None = Field(default=None, max_length=120)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalize_us_phone(v) if v else None

    @field_validator("name", "reason", "medications", "allergies", "insurance")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v, 500)


class IntakeOut(ORM):
    id: int
    appointment_id: int | None
    name: str
    created_at: UTCDateTime

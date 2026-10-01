"""SQLAlchemy models.

All timestamps are stored as naive UTC datetimes (portable across SQLite and
PostgreSQL) and serialized with a trailing ``Z`` by the API schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_uuid() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ── Lead statuses ────────────────────────────────────────────────────────────
LEAD_STATUSES = ("NEW", "CONTACTED", "QUALIFIED", "BOOKED", "FOLLOW_UP", "CONVERTED", "LOST")


class Clinic(TimestampMixin, Base):
    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    tagline: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(120))
    address_line: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(40))
    zip_code: Mapped[str] = mapped_column(String(20))
    timezone: Mapped[str] = mapped_column(String(60), default="America/Chicago")
    # {"mon": {"open": "08:00", "close": "17:00"} | None, ...}
    hours: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    insurance_accepted: Mapped[list[str]] = mapped_column(JSON, default=list)
    financing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # AI assistant / appointment / messaging settings
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    services: Mapped[list[Service]] = relationship(back_populates="clinic", cascade="all, delete-orphan")
    faqs: Mapped[list[FAQ]] = relationship(back_populates="clinic", cascade="all, delete-orphan")
    knowledge_documents: Mapped[list["KnowledgeDocument"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    waitlist_entries: Mapped[list["WaitlistEntry"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    intake_submissions: Mapped[list["IntakeSubmission"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), unique=True)
    role: Mapped[str] = mapped_column(String(40), default="staff")  # owner | front_desk | staff
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Service(TimestampMixin, Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60), default="general")
    short_description: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    price_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_note: Mapped[str] = mapped_column(String(300), default="")
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    clinic: Mapped[Clinic] = relationship(back_populates="services")


class FAQ(TimestampMixin, Base):
    __tablename__ = "faqs"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(String(300))
    answer: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60), default="general")
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    clinic: Mapped[Clinic] = relationship(back_populates="faqs")


class KnowledgeDocument(TimestampMixin, Base):
    """Uploaded or pasted text that the receptionist retrieves as RAG context."""

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="paste")  # upload | paste
    text: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    clinic: Mapped[Clinic] = relationship(back_populates="knowledge_documents")


class WaitlistEntry(TimestampMixin, Base):
    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    service: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="WAITING")  # WAITING | NOTIFIED | BOOKED | CANCELLED

    clinic: Mapped[Clinic] = relationship(back_populates="waitlist_entries")


class IntakeSubmission(TimestampMixin, Base):
    """Pre-visit intake filled from the public /intake page."""

    __tablename__ = "intake_submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"), index=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    clinic: Mapped[Clinic] = relationship(back_populates="intake_submissions")


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    service: Mapped[str | None] = mapped_column(String(120), nullable=True)
    patient_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # new | existing
    insurance: Mapped[str | None] = mapped_column(String(120), nullable=True)
    urgency: Mapped[str | None] = mapped_column(String(20), nullable=True)  # emergency | soon | normal | flexible
    preferred_date: Mapped[str | None] = mapped_column(String(80), nullable=True)
    preferred_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="NEW", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_label: Mapped[str] = mapped_column(String(10), default="COLD")  # HOT | WARM | COLD
    source: Mapped[str] = mapped_column(String(40), default="ai_chat")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversations: Mapped[list[Conversation]] = relationship(back_populates="lead")
    appointments: Mapped[list[Appointment]] = relationship(back_populates="lead")
    followups: Mapped[list[FollowUp]] = relationship(back_populates="lead", cascade="all, delete-orphan")


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_uuid)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default="web_chat")
    intent: Mapped[str] = mapped_column(String(60), default="general")
    service: Mapped[str | None] = mapped_column(String(120), nullable=True)
    stage: Mapped[str] = mapped_column(String(40), default="start")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | BOOKED | FOLLOW_UP | ABANDONED | CLOSED
    lead_score: Mapped[str] = mapped_column(String(10), default="COLD")
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    page: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lead: Mapped[Lead | None] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Appointment(TimestampMixin, Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    patient_name: Mapped[str] = mapped_column(String(120))
    patient_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    patient_email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    service: Mapped[str] = mapped_column(String(120))
    provider_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="CONFIRMED")  # CONFIRMED | COMPLETED | CANCELLED | NO_SHOW
    source: Mapped[str] = mapped_column(String(30), default="ai_chat")  # ai_chat | booking_form | phone | walk_in
    scheduler: Mapped[str] = mapped_column(String(20), default="mock")  # mock | calcom
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")

    lead: Mapped[Lead | None] = relationship(back_populates="appointments")
    followups: Mapped[list[FollowUp]] = relationship(back_populates="appointment")


class Automation(TimestampMixin, Base):
    __tablename__ = "automations"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(String(160))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | PAUSED
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    runs_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AutomationEvent(Base):
    """Execution log entry for an automation (powers the live activity feed)."""

    __tablename__ = "automation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    automation_key: Mapped[str] = mapped_column(String(60), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")  # COMPLETED | SIMULATED | SCHEDULED | FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FollowUp(TimestampMixin, Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True)
    # missed_lead | reminder_* | review_request | no_show_recovery | hygiene_recall | waitlist_open | treatment_plan
    kind: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(10))  # sms | email
    recipient: Mapped[str] = mapped_column(String(160))
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")  # SCHEDULED | SENT | SIMULATED | CANCELLED | FAILED
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="followups")
    appointment: Mapped[Appointment | None] = relationship(back_populates="followups")


class Integration(TimestampMixin, Base):
    """Per-provider config. API keys may be stored here when pasted in admin Settings.

    Values overlay environment variables at runtime. GET endpoints never return secrets.
    """

    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(40), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))  # "admin", "ai_receptionist", "system", "public"
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

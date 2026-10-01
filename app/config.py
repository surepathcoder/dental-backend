"""Application settings loaded from environment variables.

Every integration is optional. When a key is missing (or DEMO_MODE is on) the
related feature falls back to a safe, simulated implementation so the whole
platform can be demonstrated without any third-party accounts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    demo_mode: bool = True

    # Database — falls back to a local SQLite file so the demo runs anywhere.
    database_url: str = ""

    # AI providers (Groq preferred, OpenAI as alternative, rule-based fallback)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 12.0

    # Messaging
    resend_api_key: str = ""
    resend_from_email: str = "BrightSmile Dental <onboarding@resend.dev>"
    gmail_address: str = ""
    gmail_app_password: str = ""
    gmail_from_name: str = "BrightSmile Dental"
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 587
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Scheduling
    cal_api_key: str = ""
    cal_event_type_id: str = ""
    cal_api_url: str = "https://api.cal.com/v2"

    # Security
    admin_api_key: str = ""
    cors_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 120
    chat_rate_limit_per_minute: int = 30
    trust_proxy_headers: bool = True

    # Background jobs
    enable_scheduler: bool = True
    scheduler_interval_seconds: int = 60
    abandoned_chat_minutes: int = 30
    # Idle chat working memory (stage, collected fields, this-visit booking) expires after this.
    chat_session_ttl_minutes: int = 60

    public_site_url: str = "http://localhost:3000"
    clinic_timezone: str = "America/Chicago"

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_db_url(cls, v: str | None) -> str:
        v = (v or "").strip()
        if not v:
            return f"sqlite:///{(BACKEND_DIR / 'brightsmile_demo.db').as_posix()}"
        # Render / Heroku style URLs → SQLAlchemy + psycopg3 driver
        if v.startswith("postgres://"):
            v = "postgresql+psycopg://" + v[len("postgres://"):]
        elif v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_admin_key(self) -> str:
        """In demo mode a well-known key is accepted so prospects can click around.
        Outside demo mode ADMIN_API_KEY must be configured explicitly."""
        if self.admin_api_key:
            return self.admin_api_key
        return "demo-admin-key" if self.demo_mode else ""

    @property
    def llm_provider(self) -> str:
        if self.groq_api_key:
            return "groq"
        if self.openai_api_key:
            return "openai"
        return "mock"

    @property
    def gmail_ready(self) -> bool:
        return bool(self.gmail_address.strip() and self.gmail_app_password.replace(" ", ""))

    @property
    def email_provider(self) -> str:
        if self.gmail_ready:
            return "gmail"
        if self.resend_api_key:
            return "resend"
        return "simulated"

    @property
    def email_live(self) -> bool:
        # Gmail SMTP is an explicit opt-in to real delivery, including in demo mode.
        if self.gmail_ready:
            return True
        return bool(self.resend_api_key) and not self.demo_mode

    @property
    def sms_live(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_phone_number) and not self.demo_mode

    @property
    def calendar_live(self) -> bool:
        return bool(self.cal_api_key and self.cal_event_type_id) and not self.demo_mode


@lru_cache
def get_settings() -> Settings:
    base = Settings()
    if not _runtime_overrides:
        return base
    return base.model_copy(update=_runtime_overrides)


_runtime_overrides: dict[str, Any] = {}


def set_runtime_overrides(values: dict[str, Any]) -> None:
    """Apply admin-saved secrets/flags on top of .env without restarting."""
    global _runtime_overrides
    _runtime_overrides = {k: v for k, v in values.items() if v not in (None, "")}
    get_settings.cache_clear()

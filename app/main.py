"""BrightSmile Dental — AI Receptionist & Patient Automation API."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .database import Base, SessionLocal, engine
from .routers import analytics, appointments, automations, chat, clinic, conversations, demo, followups, intake, knowledge, leads, waitlist, webhooks
from .seed import ensure_seeded
from .services.automations import process_due_followups, sweep_abandoned_conversations
from .services.runtime_settings import apply_overrides

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("brightsmile")
settings = get_settings()


def _scheduler_loop(stop: threading.Event) -> None:
    """Minimal background worker: delivers due follow-ups and detects abandoned chats.
    Swap for Celery / RQ / a cron job when running multiple instances."""
    while not stop.wait(settings.scheduler_interval_seconds):
        db = SessionLocal()
        try:
            abandoned = sweep_abandoned_conversations(db)
            sent = process_due_followups(db)
            db.commit()
            if abandoned or sent:
                logger.info("scheduler: %s abandoned chats handled, %s follow-ups delivered", abandoned, sent)
        except Exception:  # keep the worker alive
            logger.exception("scheduler tick failed")
            db.rollback()
        finally:
            db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    boot = get_settings()
    if not boot.demo_mode and not boot.admin_api_key:
        logger.warning("DEMO_MODE is off but ADMIN_API_KEY is not set — admin endpoints will reject all requests.")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if ensure_seeded(db):
            logger.info("Seeded demo data")
        apply_overrides(db)
    stop = threading.Event()
    worker = None
    live = get_settings()
    if live.enable_scheduler:
        worker = threading.Thread(target=_scheduler_loop, args=(stop,), daemon=True, name="followup-scheduler")
        worker.start()
    logger.info("BrightSmile API ready · demo_mode=%s · db=%s", live.demo_mode, "sqlite" if live.is_sqlite else "postgresql")
    yield
    stop.set()


app = FastAPI(
    title="BrightSmile Dental — AI Receptionist API",
    version="1.0.0",
    description=(
        "Portfolio / demo system. AI receptionist (LangGraph), lead qualification, appointment booking, "
        "automated confirmations, reminders and follow-ups for a **fictional** dental clinic.\n\n"
        "Admin endpoints require the `X-API-Key` header. In DEMO_MODE the key `demo-admin-key` is accepted "
        "unless ADMIN_API_KEY is set."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
    max_age=600,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. Please try again."})


for r in (chat, leads, appointments, followups, conversations, analytics, automations, clinic, knowledge, demo, waitlist, intake, webhooks):
    app.include_router(r.router)


@app.get("/api/health", tags=["System"])
def health() -> dict:
    return {"status": "ok", "demo_mode": get_settings().demo_mode}

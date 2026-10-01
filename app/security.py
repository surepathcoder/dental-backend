"""Authentication, rate limiting and audit logging."""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AuditLog

logger = logging.getLogger("brightsmile.security")
settings = get_settings()


def client_ip(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


# ── Rate limiting ────────────────────────────────────────────────────────────
class SlidingWindowLimiter:
    """In-memory sliding-window limiter.

    Good enough for a single instance. For horizontal scaling swap this for a
    Redis-backed limiter (same interface).
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = int(window_seconds - (now - hits[0])) + 1
                return False, retry_after
            hits.append(now)
            if len(self._hits) > 50_000:  # crude memory guard
                self._hits.clear()
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def rate_limit(bucket: str, limit: int | None = None):
    def dependency(request: Request) -> None:
        max_hits = limit if limit is not None else settings.rate_limit_per_minute
        ok, retry_after = limiter.check(f"{bucket}:{client_ip(request)}", max_hits)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency


# ── Admin authentication ─────────────────────────────────────────────────────
def require_admin(request: Request, db: Session = Depends(get_db)) -> str:
    expected = settings.effective_admin_key
    provided = request.headers.get("x-api-key", "")
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ADMIN_API_KEY is not configured on the server.")
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        audit(db, "public", "auth.failed", ip=client_ip(request), detail={"path": request.url.path})
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key.")
    return "admin"


# ── Audit log ────────────────────────────────────────────────────────────────
def audit(
    db: Session,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record an audit entry. Caller controls the transaction.

    Never put secrets or full patient messages in ``detail``.
    """
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            ip_address=ip,
            detail=detail or {},
        )
    )
    logger.info("audit %s %s %s:%s", actor, action, entity_type, entity_id)

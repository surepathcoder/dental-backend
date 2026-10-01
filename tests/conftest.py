import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolated database + deterministic demo configuration for tests
_tmp = tempfile.mkdtemp(prefix="brightsmile-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp, 'test.db').as_posix()}"
os.environ["DEMO_MODE"] = "true"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
for key in ("GROQ_API_KEY", "OPENAI_API_KEY", "RESEND_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "TWILIO_ACCOUNT_SID", "CAL_API_KEY"):
    os.environ[key] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.security import limiter  # noqa: E402

ADMIN = {"X-API-Key": "test-admin-key"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    limiter.reset()


class Chat:
    def __init__(self, client):
        self.client = client
        self.cid = None
        self.last = None

    def say(self, message: str = "", action: dict | None = None, page: str | None = None) -> dict:
        payload: dict = {"message": message, "action": action}
        if self.cid:
            payload["conversation_id"] = self.cid
        if page:
            payload["page"] = page
        r = self.client.post("/api/chat", json=payload)
        assert r.status_code == 200, r.text
        self.last = r.json()
        self.cid = self.last["conversation_id"]
        return self.last

    def share_phone(self, number: str = "512-555-0187") -> dict:
        return self.say(number)


@pytest.fixture
def chat(client):
    return Chat(client)

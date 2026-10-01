"""Thin LLM client. Groq (preferred) or OpenAI via their OpenAI-compatible
chat completions APIs. Returns ``None`` on any failure so callers can fall
back to deterministic, knowledge-base-only answers."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger("brightsmile.llm")

_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}


def provider_label() -> str:
    s = get_settings()
    return {"groq": f"Groq · {s.groq_model}", "openai": f"OpenAI · {s.openai_model}"}.get(
        s.llm_provider, "Rule-based (demo)"
    )


def is_available() -> bool:
    return get_settings().llm_provider != "mock"


def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 350,
    json_mode: bool = False,
) -> str | None:
    s = get_settings()
    provider = s.llm_provider
    if provider == "mock":
        return None

    key = s.groq_api_key if provider == "groq" else s.openai_api_key
    model = s.groq_model if provider == "groq" else s.openai_model
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = httpx.post(
            _ENDPOINTS[provider],
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=s.llm_timeout_seconds,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return content.strip() if content else None
    except Exception as exc:  # network, auth, quota, parsing …
        logger.warning("LLM call failed (%s): %s", provider, exc)
        return None


def json_completion(messages: list[dict[str, str]], **kw: Any) -> dict[str, Any] | None:
    raw = chat_completion(messages, json_mode=True, temperature=0, **kw)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM returned invalid JSON")
        return None

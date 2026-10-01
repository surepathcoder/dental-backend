"""Golden-set eval for retrieval quality and grounded receptionist replies (mock LLM)."""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.knowledge import build_docs, retrieve
from app.agent.runner import load_clinic
from app.database import SessionLocal

CASES = json.loads((Path(__file__).parent / "eval_cases.json").read_text(encoding="utf-8"))


def test_eval_chat_replies(client):
    from conftest import Chat

    for case in CASES:
        chat = Chat(client)
        r = chat.say(case["message"])
        reply = r["reply"].lower()
        for needle in case.get("expect_reply_contains") or []:
            assert needle.lower() in reply, (case["id"], needle, r["reply"])
        any_needles = case.get("expect_reply_any") or []
        if any_needles:
            assert any(n.lower() in reply for n in any_needles), (case["id"], any_needles, r["reply"])
        for banned in case.get("forbid") or []:
            assert banned.lower() not in reply, (case["id"], banned, r["reply"])


def test_eval_retrieval_ids():
    db = SessionLocal()
    try:
        docs = build_docs(load_clinic(db))
    finally:
        db.close()
    for case in CASES:
        wanted = case.get("expect_doc_ids_any") or []
        if not wanted:
            continue
        hits = retrieve(case["message"], docs)
        ids = [d.id for d, _ in hits]
        assert any(w in ids for w in wanted), (case["id"], wanted, ids)


def test_paraphrase_insurance_and_hours():
    db = SessionLocal()
    try:
        docs = build_docs(load_clinic(db))
    finally:
        db.close()
    ins = retrieve("do yall take delta?", docs)
    assert ins and ("insurance" in ins[0][0].id or ins[0][0].category == "Insurance Question")
    hours = retrieve("when yall close on sat", docs)
    assert hours and (hours[0][0].id in {"hours", "faq:1"} or hours[0][0].category == "Hours & Location")

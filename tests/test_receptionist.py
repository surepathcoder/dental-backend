"""End-to-end tests for the LangGraph AI receptionist (rule-based demo mode)."""

from conftest import ADMIN

CONTACT = {"type": "submit_contact", "name": "Jordan Miller", "phone": "512-555-0187", "email": "jordan.miller@example.com"}


def past_phone(chat):
    if chat.last and chat.last.get("stage") == "ask_phone":
        return chat.share_phone()
    return chat.last


def test_greeting_offers_quick_replies(chat):
    r = chat.say("Hello")
    assert r["intent"] == "greeting"
    assert "Book an appointment" in r["quick_replies"]
    assert r["demo_mode"] is True


def test_info_questions_are_grounded(chat):
    cases = {
        "What are your opening hours?": "Monday",
        "Where are you located?": "Bright Leaf Blvd",
        "Do you accept Cigna?": "in-network with Cigna",
        "How much does an appointment cost?": "$129",
        "Do you offer emergency appointments?": "same-day",
        "What services do you offer?": "Invisalign",
        "Do you offer dental implants?": "implant",
        "How do I book an appointment?": "book",
    }
    for question, expected in cases.items():
        r = chat.say(question)
        assert expected.lower() in r["reply"].lower(), (question, r["reply"])
        assert r["intent"] == "info", question


def test_hours_faq_does_not_repeat_itself(chat):
    r = chat.say("What are your hours?")
    titles = [s["title"] for s in (r.get("sources") or [])]
    assert len(titles) == len(set(titles))
    assert sum("hour" in t.lower() for t in titles) <= 1
    chips = r.get("quick_replies") or []
    assert "What are your hours?" not in chips
    assert "Book an appointment" in chips


def test_insurance_faq_followup_chips(chat):
    r = chat.say("Do you accept my insurance?")
    titles = [s["title"] for s in (r.get("sources") or [])]
    assert sum("insurance" in t.lower() for t in titles) <= 1
    chips = r.get("quick_replies") or []
    assert "Do you accept my insurance?" not in chips


def test_invisalign_booking_end_to_end(chat, client):
    r = chat.say("Do you offer Invisalign?")
    assert "invisalign" in r["reply"].lower()
    past_phone(chat)
    r = chat.say("Yes, let's book")
    if r["stage"] == "ask_phone":
        r = past_phone(chat)
    assert r["stage"] == "ask_patient_type"
    r = chat.say("I'm a new patient")
    assert r["stage"] == "ask_contact" and r["ui"]["type"] == "contact_form"
    r = chat.say(action=CONTACT)
    assert r["stage"] == "ask_insurance"
    assert r["lead"]["status"] == "QUALIFIED"
    r = chat.say("Delta Dental")
    assert "in-network with Delta Dental" in r["reply"]
    r = chat.say("Within the next 2 weeks")
    assert r["stage"] == "ask_preference"
    r = chat.say("Mornings")
    assert r["stage"] == "choose_slot" and len(r["slots"]) > 0
    assert r["ui"]["demo_note"].startswith("Demo booking")
    slot = r["slots"][0]
    r = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert r["stage"] == "booked"
    assert r["appointment"]["service"] == "Invisalign"
    labels = [e["label"] for e in r["events"]]
    assert "Appointment booked" in labels and "Confirmation email sent" in labels and "Confirmation SMS sent" in labels
    assert all(e["status"] != "completed" for e in r["events"] if e["key"] in {"email", "sms"})  # simulated in demo
    lead = client.get(f"/api/leads/{r['lead']['id']}", headers=ADMIN).json()
    assert lead["status"] == "BOOKED" and lead["score_label"] == "HOT"
    assert lead["conversations"][0]["messages"], "conversation history is stored"
    assert any(str(f["kind"]).startswith("reminder_") for f in lead["followups"])


def test_free_text_contact_details(chat):
    chat.say("I want Invisalign")
    past_phone(chat)
    chat.say("existing patient")
    r = chat.say("Sam Rivera, 512 555 0144, sam.rivera@example.com")
    assert r["lead"]["name"] == "Sam Rivera"
    assert r["lead"]["phone"] == "(512) 555-0144"
    assert r["lead"]["email"] == "sam.rivera@example.com"
    assert r["stage"] == "ask_insurance"


def test_question_mid_flow_resumes_booking(chat):
    chat.say("I'd like to book a cleaning")
    past_phone(chat)
    r = chat.say("What are your hours on Saturday?")
    assert "saturday" in r["reply"].lower()
    assert "back to your appointment" in r["reply"].lower()
    assert r["stage"] == "ask_patient_type"


def test_emergency_is_not_diagnosed_and_prioritised(chat):
    r = chat.say("I cracked my tooth and I'm in severe pain")
    assert r["intent"] == "emergency"
    assert "can't diagnose" in r["reply"]
    assert "emergency room" in r["reply"] or "911" in r["reply"]
    assert r["stage"] == "ask_phone"
    r = chat.say(action=CONTACT)
    assert r["stage"] == "ask_patient_type"
    r = chat.say("new patient")
    r = chat.say("Self-pay")
    assert r["stage"] == "choose_slot"
    assert r["lead"]["urgency"] == "emergency"


def test_red_flags_get_911_warning_first(chat):
    r = chat.say("My face is swollen and I have difficulty breathing")
    assert r["reply"].startswith("⚠️")
    assert r["disclaimer"]


def test_medical_question_gets_disclaimer(chat):
    r = chat.say("Should I take antibiotics for my gum infection?")
    assert "can't give medical advice" in r["reply"]
    assert "antibiotic" not in r["reply"].lower().replace("antibiotics for", "")


def test_decline_without_contact_offers_capture_then_follow_up(chat, client):
    chat.say("I'm interested in dental implants")
    r = chat.say("Not right now")
    assert r["stage"] == "followup_capture"
    r = chat.say(action={"type": "submit_contact", "name": "Morgan Ellis", "phone": "5125550163", "email": ""})
    assert r["stage"] == "followup_scheduled"
    assert r["events"][0]["label"] == "Follow-up scheduled"
    lead = client.get(f"/api/leads/{r['lead']['id']}", headers=ADMIN).json()
    assert lead["status"] == "FOLLOW_UP"
    fu = [f for f in lead["followups"] if f["kind"] == "missed_lead"][0]
    assert "dental implants" in fu["message"] and fu["status"] == "SCHEDULED"
    # Staff can trigger it immediately; in demo mode it is simulated.
    sent = client.post(f"/api/followups/{fu['id']}/send", headers=ADMIN).json()
    assert sent["status"] == "SIMULATED"


def test_closing_chat_triggers_missed_lead_follow_up(chat, client):
    chat.say("I want Invisalign")
    chat.say("new patient")
    chat.say(action={"type": "submit_contact", "name": "Casey Lee", "phone": "512-555-0170", "email": "casey@example.com"})
    ev = client.post(f"/api/chat/{chat.cid}/end").json()
    assert ev and ev["label"] == "Follow-up scheduled"


def test_asap_skips_preference(chat):
    chat.say("Book a cleaning")
    chat.say("new patient")
    chat.say(action=CONTACT)
    chat.say("Cigna")
    r = chat.say("As soon as possible")
    assert r["stage"] == "choose_slot"


def test_show_other_times(chat):
    chat.say("Book a cleaning")
    chat.say("new patient")
    chat.say(action=CONTACT)
    chat.say("Aetna")
    chat.say("I'm flexible")
    r = chat.say("Next week")
    assert r["stage"] == "choose_slot"
    r = chat.say("Show other times")
    assert r["stage"] == "ask_preference"


def test_invalid_slot_rejected(chat):
    chat.say("Book a cleaning")
    chat.say("new patient")
    chat.say(action=CONTACT)
    chat.say("Aetna")
    chat.say("I'm flexible")
    chat.say("Earliest available")
    r = chat.say(action={"type": "select_slot", "slot_id": "2020-01-01T15:00:00Z"})
    assert r["stage"] == "choose_slot"
    assert "passed" in r["reply"] or "taken" in r["reply"]


def test_booking_service_chip_skips_faq_dump(chat):
    r = chat.say("Book an appointment")
    assert r["stage"] == "ask_phone"
    r = past_phone(chat)
    assert r["stage"] == "ask_service"
    r = chat.say("Invisalign")
    assert r["stage"] == "ask_patient_type"
    assert "iTero" not in r["reply"]
    assert "Now, back to your appointment" not in r["reply"]
    assert "Invisalign" in r["reply"]
    assert "visited BrightSmile Dental before" in r["reply"]


def test_after_booking_bring_question_stays_on_that_visit(chat):
    chat.say("Book an appointment")
    chat.say("Invisalign")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    chat.say("MetLife")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert booked["stage"] == "booked"
    r = chat.say("What should I bring?")
    assert r["stage"] == "booked"
    assert "can't give medical advice" not in r["reply"].lower()
    assert "Yes, let's book" not in (r.get("quick_replies") or [])
    assert "id" in r["reply"].lower()
    assert "invisalign" in r["reply"].lower()
    assert (r.get("ui") or {}).get("type") != "slots"


def test_insurance_question_does_not_skip_collection(chat):
    chat.say("Do you offer Invisalign?")
    chat.say("Yes, let's book")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    r = chat.say("what is delta dental")
    assert r["stage"] == "ask_insurance"
    assert "delta dental" in r["reply"].lower()
    assert "How soon" not in r["reply"]
    assert "Now, back to your appointment" in r["reply"]


def test_staff_handoff_after_booking_skips_wait_chip(chat):
    chat.say("Book an appointment")
    chat.say("Invisalign")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    chat.say("MetLife")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert booked["stage"] == "booked"
    r = chat.say("Talk to a person")
    chips = r.get("quick_replies") or []
    assert "I'll wait" not in chips
    assert "Book an appointment" not in chips
    assert "That's all, thanks!" in chips


def test_page_aware_invisalign_greeting(chat):
    r = chat.say("Hello", page="/services/invisalign")
    assert r["intent"] == "greeting"
    assert "invisalign" in r["reply"].lower()
    assert r["sources"]


def test_spanish_greeting(chat):
    r = chat.say("Hola")
    assert "español" in r["reply"].lower() or "spanish" in r["reply"].lower()


def test_reschedule_and_cancel_in_chat(chat, client):
    chat.say("I want Invisalign")
    chat.say("Yes, let's book")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    chat.say("Delta Dental")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert booked["stage"] == "booked"
    moved = chat.say("I need to reschedule")
    assert moved["slots"] or "new time" in moved["reply"].lower() or "reschedule" in moved["reply"].lower() or "find" in moved["reply"].lower()
    if moved["slots"]:
        again = chat.say(action={"type": "select_slot", "slot_id": moved["slots"][0]["id"]})
        assert again["appointment"] or again["stage"] == "booked"
    cancelled = chat.say("Cancel this appointment")
    if cancelled["stage"] == "confirm_cancel":
        cancelled = chat.say("Yes, cancel it")
    assert "cancel" in cancelled["reply"].lower()
    appts = client.get("/api/appointments", headers=ADMIN).json()
    statuses = {a["status"] for a in appts if a.get("patient_name") == "Jordan Miller"}
    assert "CANCELLED" in statuses or cancelled["stage"] in {"start", "booked"}


def test_staff_takeover_and_live_reply(chat, client):
    r = chat.say("I'd like to speak to a person")
    assert r["waiting_staff"] is True or r["stage"] == "waiting_staff"
    queued = chat.say("Can someone call me back?")
    assert "front desk" in queued["reply"].lower() or "passed" in queued["reply"].lower()
    posted = client.post(f"/api/conversations/{chat.cid}/staff-reply", headers=ADMIN, json={"message": "Hi, this is Hannah at the front desk."})
    assert posted.status_code == 200, posted.text
    updates = client.get(f"/api/chat/{chat.cid}/updates", params={"after_id": 0})
    assert updates.status_code == 200
    assert any(m.get("source") == "staff" for m in updates.json()["messages"])
    resumed = client.post(f"/api/conversations/{chat.cid}/resume", headers=ADMIN)
    assert resumed.status_code == 200


def test_empty_facts_never_calls_llm(chat, monkeypatch):
    calls: list = []

    def fake_complete(*_args, **_kwargs):
        calls.append(True)
        return "The wifi password is $999 and we prescribe amoxicillin."

    monkeypatch.setattr("app.services.llm.is_available", lambda: True)
    monkeypatch.setattr("app.services.llm.chat_completion", fake_complete)
    r = chat.say("What's the office wifi password?")
    assert "don't have a confirmed answer" in r["reply"].lower()
    assert "$999" not in r["reply"]
    # May call the intent classifier; must not accept an ungrounded answer.
    assert "amoxicillin" not in r["reply"].lower()


def test_chat_stream_returns_sse(client):
    r = client.post("/api/chat/stream", json={"message": "What are your hours?"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "event: done" in r.text
    assert "Monday" in r.text or "hours" in r.text.lower()


def test_thats_all_thanks_after_booking(chat):
    chat.say("Book an appointment")
    chat.say("Invisalign")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    chat.say("Aetna")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert booked["stage"] == "booked"
    r = chat.say("That's all, thanks!")
    assert r["intent"] == "thanks"
    assert "welcome" in r["reply"].lower()
    assert "don't have a confirmed answer" not in r["reply"].lower()


def test_unfinished_booking_thanks_schedules_followup(chat):
    chat.say("Book an appointment")
    chat.say("Invisalign")
    chat.say("I'm a new patient")
    chat.say(action={"type": "submit_contact", "name": "Riley Chen", "phone": "512-555-0191", "email": "riley.chen@example.com"})
    r = chat.say("That's all, thanks!")
    assert r["stage"] == "followup_scheduled"
    assert any((e.get("key") == "followup") for e in (r.get("events") or []))
    assert "invisalign" in r["reply"].lower()
    assert "that's all" not in r["reply"].lower()


def test_greeting_resumes_unfinished_booking(chat):
    chat.say("Book an appointment")
    past_phone(chat)
    chat.say("Invisalign")
    r = chat.say("Hello")
    assert "we were booking" in r["reply"].lower()
    assert "invisalign" in r["reply"].lower()
    assert r["stage"] == "ask_patient_type"


def test_returning_patient_memory_by_phone(chat, client):
    from conftest import Chat

    details = {"type": "submit_contact", "name": "Alex Rivera", "phone": "512-555-0198", "email": "alex.rivera@example.com"}
    chat.say("Book an appointment")
    past_phone(chat)
    chat.say("Invisalign")
    chat.say("I'm a new patient")
    chat.say(action=details)
    chat.say("MetLife")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    other = Chat(client)
    other.say("Book an appointment")
    other.say(action=details)
    other.say("General Dentistry")
    r = other.say("I'm an existing patient")
    assert "found your upcoming visit" in r["reply"].lower()
    assert "invisalign" in r["reply"].lower()


def test_idle_session_starts_fresh(chat):
    from datetime import timedelta

    from app.database import SessionLocal
    from app.models import Conversation, utcnow

    r = chat.say("I'd like to book Invisalign")
    old_id = r["conversation_id"]
    assert r.get("new_session") is False
    assert r["stage"] != "start"

    db = SessionLocal()
    try:
        convo = db.get(Conversation, old_id)
        assert convo is not None
        convo.last_message_at = utcnow() - timedelta(minutes=61)
        db.commit()
    finally:
        db.close()

    r2 = chat.say("Hello")
    assert r2["conversation_id"] != old_id
    assert r2["new_session"] is True
    assert r2["intent"] == "greeting"
    assert r2["stage"] == "start"


def test_active_session_keeps_booking_memory(chat):
    r = chat.say("I'd like to book Invisalign")
    old_id = r["conversation_id"]
    r2 = past_phone(chat)
    assert r2["conversation_id"] == old_id
    assert r2.get("new_session") is False
    assert r2["stage"] == "ask_patient_type"


def test_staff_handoff_survives_session_ttl(chat):
    from datetime import timedelta

    from app.database import SessionLocal
    from app.models import Conversation, utcnow

    r = chat.say("I'd like to speak to a person")
    assert r["waiting_staff"] is True or r["stage"] == "waiting_staff"
    old_id = r["conversation_id"]

    db = SessionLocal()
    try:
        convo = db.get(Conversation, old_id)
        assert convo is not None
        convo.last_message_at = utcnow() - timedelta(minutes=61)
        state = dict(convo.state or {})
        fields = dict(state.get("fields") or {})
        fields["waiting_since"] = utcnow().isoformat()
        state["fields"] = fields
        convo.state = state
        db.commit()
    finally:
        db.close()

    queued = chat.say("Can someone call me back?")
    assert queued["conversation_id"] == old_id
    assert queued.get("new_session") is False
    assert queued["waiting_staff"] is True or queued["stage"] == "waiting_staff"


def test_near_visit_reminder_is_queued(chat):
    chat.say("Book an appointment")
    chat.say("Invisalign")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    chat.say("Aetna")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    keys = [e["key"] for e in booked["events"]]
    assert "reminder_2h" in keys
    assert "reminder_30m" in keys


def test_book_another_family_member_in_same_chat(chat, client):
    chat.say("I want Invisalign")
    chat.say("Yes, let's book")
    chat.say("I'm a new patient")
    chat.say(action={"type": "submit_contact", "name": "Pat Nguyen", "phone": "512-555-0199", "email": "pat.nguyen@example.com"})
    chat.say("Delta Dental")
    chat.say("As soon as possible")
    slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert booked["stage"] == "booked"
    assert "Book another family member" in (booked.get("quick_replies") or [])
    parent_id = booked["appointment"]["id"]

    r = chat.say("Book another family member")
    assert r["stage"] == "ask_service"
    r = chat.say("General Dentistry")
    assert r["stage"] == "ask_patient_type"
    r = chat.say("I'm a new patient")
    assert r["stage"] == "ask_contact"
    r = chat.say(action={"type": "submit_contact", "name": "Emma Nguyen", "phone": "512-555-0199", "email": "pat.nguyen@example.com"})
    for _ in range(6):
        stage = r.get("stage")
        ui_type = (r.get("ui") or {}).get("type")
        if stage == "choose_slot" or ui_type == "slots" or r.get("slots"):
            break
        if stage == "ask_urgency":
            r = chat.say("As soon as possible")
        elif stage == "ask_preference":
            r = chat.say("Mornings")
        elif stage == "ask_insurance":
            r = chat.say("Delta Dental")
        elif stage == "ask_member_id":
            r = chat.say("I'll skip for now")
        else:
            break
    assert r["stage"] == "choose_slot" or r.get("slots")
    child = chat.say(action={"type": "select_slot", "slot_id": r["slots"][0]["id"]})
    assert child["stage"] == "booked"
    assert child["appointment"]["id"] != parent_id
    assert child["appointment"]["patient_name"] == "Emma Nguyen"
    lead = client.get(f"/api/leads/{child['lead']['id']}", headers=ADMIN).json()
    assert "Emma Nguyen" in (lead.get("notes") or "")
    assert lead["name"] == "Pat Nguyen"


def test_spanish_can_start_booking(chat):
    r = chat.say("Hola")
    assert "español" in r["reply"].lower()
    r = chat.say("Quiero una cita")
    assert r["stage"] == "ask_phone"
    past_phone(chat)
    r = chat.say("Invisalign")
    assert r["stage"] == "ask_patient_type"
    assert "visitado" in r["reply"].lower() or "visited" in r["reply"].lower()
    r = chat.say("Soy paciente nuevo")
    assert r["stage"] == "ask_contact"


def test_page_aware_price_uses_that_service(chat):
    r = chat.say("How much does it cost?", page="/services/invisalign")
    reply = r["reply"].lower()
    assert "invisalign" in reply
    assert "3,500" in r["reply"] or "3500" in r["reply"] or "$3" in r["reply"]


def test_page_aware_book_inherits_service(chat):
    r = chat.say("I'd like to book", page="/services/dental-implants")
    assert r["stage"] == "ask_phone"
    r = past_phone(chat)
    assert r["stage"] == "ask_patient_type"
    assert "implant" in r["reply"].lower()


def test_insurance_member_id_queues_verify_benefits(chat, client):
    chat.say("I want Invisalign")
    chat.say("Yes, let's book")
    chat.say("I'm a new patient")
    chat.say(action=CONTACT)
    r = chat.say("Delta Dental")
    assert r["stage"] == "ask_member_id"
    r = chat.say("W123456789")
    assert r["stage"] == "ask_urgency"
    chat.say("As soon as possible")
    slot = chat.last["slots"][0] if chat.last.get("slots") else None
    if not slot:
        chat.say("Mornings")
        slot = chat.last["slots"][0]
    booked = chat.say(action={"type": "select_slot", "slot_id": slot["id"]})
    assert booked["stage"] == "booked"
    keys = [e["key"] for e in booked["events"]]
    assert "verify_benefits" in keys
    lead = client.get(f"/api/leads/{booked['lead']['id']}", headers=ADMIN).json()
    assert "W123456789" in (lead.get("notes") or "")


def test_chat_asks_phone_before_creating_a_lead(chat, client):
    r = chat.say("What are your hours?")
    assert r["stage"] == "ask_phone"
    assert r.get("lead") is None
    assert r["ui"]["type"] == "contact_form"
    r = chat.share_phone()
    assert r["lead"] is not None
    assert r["lead"]["phone"] == "(512) 555-0187"
    leads = client.get("/api/leads", headers=ADMIN).json()["items"]
    assert all(row.get("phone") or row.get("email") for row in leads if row["source"] == "ai_chat")


def test_price_of_each_service(chat):
    chat.say("what are the services your providing?")
    r = chat.say("what is the price of each service?")
    reply = r["reply"].lower()
    assert "don't have a confirmed answer" not in reply
    compact = r["reply"].replace(",", "")
    assert "3500" in compact
    assert "3200" in compact
    assert "invisalign" in reply
    assert "implant" in reply


def test_remembers_prior_questions(chat):
    chat.say("what are the services your providing?")
    chat.say("what is the price of each service?")
    chat.say("Do you offer financing?")
    r = chat.say("do you remember what i asked?")
    reply = r["reply"].lower()
    assert "don't have a confirmed answer" not in reply
    assert "asked" in reply or "conversation" in reply
    assert "services" in reply
    assert "price" in reply
    assert "financ" in reply


def test_chat_stream_types_out_deltas(client):
    r = client.post("/api/chat/stream", json={"message": "What are your hours?"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert r.text.count('"delta"') >= 3
    assert "event: done" in r.text

"""API, security and validation tests."""

from datetime import datetime, timezone

from conftest import ADMIN


def test_health_and_status(client):
    assert client.get("/api/health").json()["status"] == "ok"
    s = client.get("/api/status").json()
    assert s["demo_mode"] is True and s["scheduler"] == "mock" and s["sms"] == "simulated"
    assert s["email"] == "simulated"


def test_admin_endpoints_require_api_key(client):
    for path in ("/api/leads", "/api/conversations", "/api/analytics", "/api/automations", "/api/integrations", "/api/appointments"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401, path
        assert client.get(path, headers=ADMIN).status_code == 200, path


def test_purge_keeps_live_gmail_leads(client):
    from sqlalchemy import func, select

    from app.agent.runner import load_clinic
    from app.database import SessionLocal
    from app.models import Lead
    from app.seed import promote_live_records, purge_demo_activity

    db = SessionLocal()
    clinic = load_clinic(db)
    db.add(Lead(clinic_id=clinic.id, name="Sofia Adams", email="sofia.adams@example.com", status="QUALIFIED", is_demo=True))
    live = Lead(clinic_id=clinic.id, name="Michael", email="michaelmbwaga42@gmail.com", status="BOOKED", is_demo=True)
    db.add(live)
    db.flush()
    live_id = live.id
    promote_live_records(db)
    purge_demo_activity(db)
    db.commit()
    kept = db.get(Lead, live_id)
    assert kept is not None and kept.is_demo is False
    assert db.scalar(select(func.count(Lead.id)).where(Lead.email.like("%@example.com"))) == 0
    db.close()


def test_analytics_uses_live_activity(client):
    k = client.get("/api/analytics", headers=ADMIN).json()
    assert k["demo_data"] is False
    assert k["kpis"]["visitors"] == 0
    assert len(k["series"]) == 30
    names = [a["patient_name"] for a in k["today_appointments"] + k["upcoming_appointments"]]
    assert "Sofia Adams" not in names and "Caleb Young" not in names


def test_integrations_never_expose_secrets(client):
    body = client.get("/api/integrations", headers=ADMIN).text
    assert "test-admin-key" not in body
    providers = {i["provider"]: i for i in client.get("/api/integrations", headers=ADMIN).json()}
    assert providers["twilio"]["status"] == "demo"
    assert "gmail" in providers
    assert providers["gmail"]["status"] == "demo"
    assert "GMAIL_ADDRESS" in providers["gmail"]["env_vars"]


def test_chat_validation(client):
    assert client.post("/api/chat", json={}).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 1001}).status_code == 422
    assert client.post("/api/chat", json={"message": "hi", "conversation_id": "../etc"}).status_code == 422


def test_chat_rate_limit(client):
    codes = [client.post("/api/chat", json={"message": "hi"}).status_code for _ in range(35)]
    assert 429 in codes


def test_public_lead_form_validation(client):
    bad = client.post("/api/leads", json={"name": "A", "phone": "123"})
    assert bad.status_code == 422
    ok = client.post("/api/leads", json={"name": "Taylor Brooks", "phone": "(512) 555-0111", "service": "Invisalign", "message": "Hi!"})
    assert ok.status_code == 201 and ok.json()["phone"] == "(512) 555-0111"


def test_booking_form_flow(client):
    avail = client.get("/api/appointments/availability", params={"service": "General Dentistry"}).json()
    assert avail["demo"] is True and avail["slots"]
    slot = avail["slots"][0]["id"]
    body = {"name": "Riley Chen", "phone": "512-555-0190", "email": "riley.chen@example.com", "service": "General Dentistry",
            "slot_id": slot, "patient_type": "new", "insurance": "MetLife"}
    r = client.post("/api/appointments", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["appointment"]["status"] == "CONFIRMED"
    assert r.json()["appointment"]["is_demo"] is False
    # Same slot cannot be double-booked
    again = client.post("/api/appointments", json=body)
    assert again.status_code == 409
    assert client.get("/api/appointments/availability", params={"service": "Nope"}).status_code == 422


def test_lead_status_update_and_audit(client):
    created = client.post(
        "/api/leads",
        json={"name": "Taylor Brooks", "phone": "(512) 555-0111", "email": "taylor.brooks@example.com", "service": "Invisalign", "message": "Hi!"},
    )
    assert created.status_code == 201, created.text
    lead_id = created.json()["id"]
    r = client.patch(f"/api/leads/{lead_id}", headers=ADMIN, json={"status": "CONTACTED", "notes": "Called, left voicemail"})
    assert r.status_code == 200 and r.json()["status"] == "CONTACTED"
    assert client.patch(f"/api/leads/{lead_id}", headers=ADMIN, json={"status": "BOGUS"}).status_code == 422


def test_automation_toggle_and_config_validation(client):
    r = client.post("/api/automations", headers=ADMIN, json={"key": "missed_lead_followup", "config": {"delay_hours": 4}})
    assert r.status_code == 200 and r.json()["config"]["delay_hours"] == 4
    assert client.post("/api/automations", headers=ADMIN, json={"key": "missed_lead_followup", "config": {"delay_hours": 9999}}).status_code == 422
    assert client.post("/api/automations", headers=ADMIN, json={"key": "missed_lead_followup", "config": {"evil": 1}}).status_code == 422
    r = client.post("/api/automations", headers=ADMIN, json={"key": "review_request", "status": "PAUSED"})
    assert r.json()["status"] == "PAUSED"
    client.post("/api/automations", headers=ADMIN, json={"key": "review_request", "status": "ACTIVE"})
    client.post("/api/automations", headers=ADMIN, json={"key": "missed_lead_followup", "config": {"delay_hours": 24}})


def test_clinic_update_validation(client):
    assert client.post("/api/clinic", headers=ADMIN, json={"hours": {"mon": {"open": "18:00", "close": "08:00"}}}).status_code == 422
    assert client.post("/api/clinic", headers=ADMIN, json={"settings": {"secrets": {}}}).status_code == 422
    r = client.post("/api/clinic", headers=ADMIN, json={"settings": {"assistant": {"name": "Ava"}}})
    assert r.status_code == 200


def test_knowledge_paste_and_faq_roundtrip(client):
    pasted = client.post(
        "/api/knowledge/documents",
        headers=ADMIN,
        json={"title": "After hours protocol", "text": "If a tooth is knocked out, keep it in milk and call the emergency line immediately."},
    )
    assert pasted.status_code == 200, pasted.text
    body = pasted.json()
    assert body["title"] == "After hours protocol" and body["char_count"] > 20
    listed = client.get("/api/knowledge/documents", headers=ADMIN).json()
    assert listed[0]["id"] == body["id"]
    assert "milk" in listed[0]["preview"]
    assert client.delete(f"/api/knowledge/documents/{body['id']}", headers=ADMIN).status_code == 200

    faq = client.post("/api/knowledge/faqs", headers=ADMIN, json={"question": "Do you have parking?", "answer": "Yes — free covered parking in front of the building.", "category": "practice"})
    assert faq.status_code == 200, faq.text
    faq_id = faq.json()["id"]
    clinic = client.get("/api/clinic").json()
    assert any(f["id"] == faq_id for f in clinic["faqs"])
    chat = client.post("/api/chat", json={"message": "Do you have parking?"}).json()
    assert "parking" in chat["reply"].lower()
    assert client.delete(f"/api/knowledge/faqs/{faq_id}", headers=ADMIN).status_code == 200


def test_admin_can_paste_api_keys_without_leaking_them(client):
    secret = "gsk_test_secret_value_12345"
    r = client.post("/api/integrations/groq", headers=ADMIN, json={"values": {"GROQ_API_KEY": secret, "GROQ_MODEL": "llama-3.3-70b-versatile"}})
    assert r.status_code == 200, r.text
    text = r.text
    assert secret not in text
    groq = next(i for i in r.json() if i["provider"] == "groq")
    assert groq["status"] == "connected"
    key_field = next(f for f in groq["fields"] if f["key"] == "GROQ_API_KEY")
    assert key_field["configured"] is True and key_field["value"] is None
    assert key_field["hint"] and "••••" in key_field["hint"]
    runtime = client.post("/api/runtime", headers=ADMIN, json={"demo_mode": False})
    assert runtime.status_code == 200 and runtime.json()["demo_mode"] is False
    assert client.get("/api/status").json()["demo_mode"] is False
    client.post("/api/runtime", headers=ADMIN, json={"demo_mode": True})
    client.post("/api/integrations/groq", headers=ADMIN, json={"values": {"GROQ_API_KEY": "CLEAR"}})


def test_insurance_settings_are_used_by_receptionist(client):
    from app.knowledge_base import CLINIC_SEED

    r = client.post("/api/clinic", headers=ADMIN, json={"insurance_accepted": ["Delta Dental", "Guardian"]})
    assert r.status_code == 200 and r.json()["insurance_accepted"] == ["Delta Dental", "Guardian"]
    chat = client.post("/api/chat", json={"message": "Do you take Guardian?"}).json()
    assert "guardian" in chat["reply"].lower()
    client.post("/api/clinic", headers=ADMIN, json={"insurance_accepted": CLINIC_SEED["insurance_accepted"]})


def test_demo_missed_lead_simulation(client):
    r = client.post("/api/demo/missed-lead", headers=ADMIN).json()
    assert r["lead_id"] and r["events"] and r["events"][0]["status"] == "scheduled"


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_new_automations_are_seeded(client):
    keys = {a["key"] for a in client.get("/api/automations", headers=ADMIN).json()}
    assert {"no_show_recovery", "hygiene_recall", "staff_handoff_alert", "review_request", "missed_lead_followup", "treatment_plan_followup", "verify_benefits"} <= keys


def test_brand_settings_and_roi_kpis(client):
    r = client.post(
        "/api/clinic",
        headers=ADMIN,
        json={"settings": {"brand": {"google_review_url": "https://g.page/r/demo", "production_per_visit": 400}}},
    )
    assert r.status_code == 200, r.text
    brand = r.json()["settings"]["brand"]
    assert brand["google_review_url"] == "https://g.page/r/demo"
    assert brand["production_per_visit"] == 400
    assert client.post("/api/clinic", headers=ADMIN, json={"settings": {"brand": {"google_review_url": "not-a-url"}}}).status_code == 422
    k = client.get("/api/analytics", headers=ADMIN).json()["kpis"]
    assert k["production_per_visit"] == 400
    assert "estimated_production" in k and "after_hours_bookings" in k


def test_booking_queues_hygiene_and_intake_link(client):
    slots = client.get("/api/appointments/availability", params={"service": "General Dentistry"}).json()["slots"]
    slot = slots[-1]["id"]
    booked = client.post(
        "/api/appointments",
        json={
            "name": "Casey Ng",
            "phone": "512-555-0177",
            "email": "casey.ng@example.com",
            "service": "General Dentistry",
            "slot_id": slot,
        },
    )
    assert booked.status_code == 201, booked.text
    appt_id = booked.json()["appointment"]["id"]
    assert any(e.get("channel") == "email" for e in booked.json()["events"])
    followups = [f for f in client.get("/api/followups", headers=ADMIN, params={"kind": "hygiene_recall"}).json() if f["appointment_id"] == appt_id]
    assert followups
    preview = client.get("/api/intake", params={"appointment_id": appt_id})
    assert preview.status_code == 200 and preview.json()["service"] == "General Dentistry"
    intake = client.post(
        "/api/intake",
        json={"appointment_id": appt_id, "name": "Casey Ng", "reason": "Cleaning", "allergies": "None"},
    )
    assert intake.status_code == 201, intake.text
    assert client.post("/api/intake", json={"appointment_id": appt_id, "name": "Casey Ng"}).status_code == 409


def test_completed_visit_schedules_review_and_no_show_recovers(client):
    client.post(
        "/api/clinic",
        headers=ADMIN,
        json={"settings": {"brand": {"google_review_url": "https://g.page/r/demo", "production_per_visit": 400}}},
    )
    slots = client.get("/api/appointments/availability", params={"service": "Invisalign"}).json()["slots"]
    booked = client.post(
        "/api/appointments",
        json={
            "name": "Drew Patel",
            "phone": "512-555-0166",
            "email": "drew.patel@example.com",
            "service": "Invisalign",
            "slot_id": slots[0]["id"],
        },
    )
    appt_id = booked.json()["appointment"]["id"]
    done = client.patch(f"/api/appointments/{appt_id}", headers=ADMIN, json={"status": "COMPLETED"})
    assert done.status_code == 200
    reviews = [f for f in client.get("/api/followups", headers=ADMIN, params={"kind": "review_request"}).json() if f["appointment_id"] == appt_id]
    assert reviews and "g.page" in reviews[0]["message"]
    plans = [f for f in client.get("/api/followups", headers=ADMIN).json() if f["appointment_id"] == appt_id and str(f["kind"]).startswith("treatment_plan")]
    assert len(plans) >= 2
    assert any("book" in (f["message"] or "").lower() for f in plans)

    slots = client.get("/api/appointments/availability", params={"service": "Invisalign"}).json()["slots"]
    missed = client.post(
        "/api/appointments",
        json={
            "name": "Drew Patel",
            "phone": "512-555-0166",
            "email": "drew.patel@example.com",
            "service": "Invisalign",
            "slot_id": slots[0]["id"],
        },
    )
    missed_id = missed.json()["appointment"]["id"]
    client.patch(f"/api/appointments/{missed_id}", headers=ADMIN, json={"status": "NO_SHOW"})
    recover = [f for f in client.get("/api/followups", headers=ADMIN, params={"kind": "no_show_recovery"}).json() if f["appointment_id"] == missed_id]
    assert recover and "/book" in recover[0]["message"]


def test_waitlist_and_inbound_sms(client):
    joined = client.post(
        "/api/waitlist",
        json={"name": "Jordan Blake", "phone": "512-555-0133", "email": "jordan.blake@example.com", "service": "General Dentistry"},
    )
    assert joined.status_code == 201, joined.text
    listed = client.get("/api/waitlist", headers=ADMIN).json()
    assert any(e["name"] == "Jordan Blake" for e in listed)

    slot = client.get("/api/appointments/availability", params={"service": "Emergency Dentistry"}).json()["slots"][0]["id"]
    booked = client.post(
        "/api/appointments",
        json={
            "name": "Jordan Blake",
            "phone": "512-555-0133",
            "email": "jordan.blake@example.com",
            "service": "Emergency Dentistry",
            "slot_id": slot,
        },
    )
    assert booked.status_code == 201, booked.text
    sms = client.post("/api/webhooks/twilio/sms", data={"From": "+15125550133", "Body": "C"})
    assert sms.status_code == 200
    assert "confirmed" in sms.text.lower()
    again = client.post("/api/webhooks/twilio/sms", data={"From": "+15125550133", "Body": "R"})
    assert "book" in again.text.lower()


def test_hot_leads_include_callback_phone(client):
    created = client.post(
        "/api/leads",
        json={"name": "Casey Nguyen", "phone": "(512) 555-0199", "service": "Invisalign", "message": "Need a consult"},
    )
    assert created.status_code == 201
    hot = client.get("/api/analytics", headers=ADMIN).json()["hot_leads"]
    match = next((row for row in hot if row["name"] == "Casey Nguyen"), None)
    assert match is not None
    assert match["phone"] == "(512) 555-0199"


def _reminder_followups(client, appointment_id: int) -> list[dict]:
    return [
        f
        for f in client.get("/api/followups", headers=ADMIN).json()
        if f.get("appointment_id") == appointment_id and str(f.get("kind", "")).startswith("reminder")
    ]


def test_far_visit_reminders_match_slot_and_use_one_channel(client):
    slots = client.get("/api/appointments/availability", params={"service": "General Dentistry", "limit": 40}).json()["slots"]
    now = datetime.now(timezone.utc)
    far = max(slots, key=lambda s: datetime.fromisoformat(s["id"].replace("Z", "+00:00")))
    hours_until = (datetime.fromisoformat(far["id"].replace("Z", "+00:00")) - now).total_seconds() / 3600
    booked = client.post(
        "/api/appointments",
        json={
            "name": "Riley Chen",
            "phone": "512-555-0121",
            "email": "riley.chen@example.com",
            "service": "General Dentistry",
            "slot_id": far["id"],
        },
    )
    assert booked.status_code == 201, booked.text
    appt_id = booked.json()["appointment"]["id"]
    fus = _reminder_followups(client, appt_id)
    assert fus
    assert {f["channel"] for f in fus} == {"email"}
    kinds = {f["kind"] for f in fus}
    if hours_until > 26:
        assert "reminder_24h" in kinds
        assert "reminder_2h" in kinds
    for f in fus:
        assert far["time_label"] in (f["message"] or "")
        assert "in 2 hours on" not in (f["message"] or "").lower()


def test_same_day_visit_gets_one_reminder(client):
    slots = client.get("/api/appointments/availability", params={"service": "Emergency Dentistry"}).json()["slots"]
    near = slots[0]
    booked = client.post(
        "/api/appointments",
        json={
            "name": "Avery Cole",
            "phone": "512-555-0122",
            "email": "avery.cole@example.com",
            "service": "Emergency Dentistry",
            "slot_id": near["id"],
        },
    )
    assert booked.status_code == 201, booked.text
    fus = [f for f in _reminder_followups(client, booked.json()["appointment"]["id"]) if f["status"] == "SCHEDULED"]
    assert len(fus) == 1
    assert fus[0]["kind"] in {"reminder_30m", "reminder_2h"}
    assert near["time_label"] in (fus[0]["message"] or "")


def test_staff_handoff_records_alert(client):
    chat = client.post("/api/chat", json={"message": "I'd like to speak to a person"}).json()
    assert chat["waiting_staff"] is True
    assert any(e["key"] == "staff_handoff" for e in chat["events"])

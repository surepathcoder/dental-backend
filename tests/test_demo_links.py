"""Private demos for prospects: their facts only, the assistant answers as them, nothing real is ever sent."""

from conftest import ADMIN

NDOVU = {
    "name": "Ndovu Dental Clinic", "phone": "+255 759 500 717", "address": "Mikocheni, Dar es Salaam",
    "city": "Dar es Salaam", "country": "Tanzania", "timezone": "Africa/Dar_es_Salaam",
    "hours": {"mon": {"open": "08:30", "close": "18:00"}, "sun": None},
    "website": "ndovudentalclinic.co.tz", "language": "sw", "prepared_by": "Michael Mbwaga, Surepathcoder",
    "external_ref": "lead-ndovu-1",
}


def _make(client, body=NDOVU):
    r = client.post("/api/demo-links", headers=ADMIN, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_only_the_seller_can_make_a_demo(client):
    assert client.post("/api/demo-links", json=NDOVU).status_code == 401


def test_a_demo_shows_their_facts_and_nothing_invented(client):
    made = _make(client)
    assert made["path"] == f"/d/{made['token']}"
    demo = client.get(f"/api/demo-links/{made['token']}").json()
    clinic = demo["clinic"]
    assert clinic["name"] == "Ndovu Dental Clinic" and clinic["phone"] == "+255 759 500 717"
    assert clinic["insurance_accepted"] == [] and clinic["financing"] == {}
    assert all(s["price_from"] is None and not s["price_note"] for s in clinic["services"])
    assert clinic["hours"]["mon"] == {"open": "08:30", "close": "18:00"} and clinic["hours"]["sun"] is None
    assert demo["demo"]["hours_known"] is True and demo["demo"]["prepared_by"].startswith("Michael")
    # The same lead gets the same link back, not a second demo.
    assert _make(client)["token"] == made["token"]
    # The practice's own site is untouched.
    assert client.get("/api/clinic").json()["name"] != "Ndovu Dental Clinic"


def test_the_assistant_answers_as_the_demo_clinic(client):
    token = _make(client)["token"]
    r = client.post("/api/chat", json={"message": "Who are your dentists?", "demo": token})
    assert r.status_code == 200, r.text
    reply = r.json()["reply"] if "reply" in r.json() else str(r.json())
    assert "Emily Carter" not in reply and "BrightSmile" not in reply
    r = client.post("/api/chat", json={"message": "I have severe swelling and can't breathe", "demo": token})
    assert "911" not in str(r.json())


def test_unknown_or_bad_links_are_refused(client):
    assert client.get("/api/demo-links/doesnotexist123").status_code == 404
    assert client.post("/api/chat", json={"message": "hi", "demo": "x"}).status_code == 422


def test_a_visitor_abroad_can_leave_their_own_number(client):
    token = _make(client)["token"]
    first = client.post("/api/chat", json={"message": "What are your opening hours?", "demo": token}).json()
    assert "Emergency appointments are reserved" not in str(first)
    r = client.post("/api/chat", json={"conversation_id": first["conversation_id"], "action": {
        "type": "submit_contact", "name": "Amina Juma", "phone": "+255 712 345 678"}})
    assert r.status_code == 200, r.text
    assert "valid" not in str(r.json()).lower() or "phone" not in str(r.json()).lower()


def test_a_link_card_fetch_is_not_counted_as_an_open(client):
    made = _make(client, {**NDOVU, "external_ref": "lead-ndovu-peek"})
    token = made["token"]
    before = client.get(f"/api/demo-links/{token}/stats", headers=ADMIN).json()["views"]
    assert client.get(f"/api/demo-links/{token}?peek=true").json()["clinic"]["name"] == "Ndovu Dental Clinic"
    assert client.get(f"/api/demo-links/{token}/stats", headers=ADMIN).json()["views"] == before
    client.get(f"/api/demo-links/{token}")
    assert client.get(f"/api/demo-links/{token}/stats", headers=ADMIN).json()["views"] == before + 1


def test_tanzanian_numbers_are_kept_as_written():
    from app.schemas import normalize_us_phone as norm
    assert norm("(512) 555-0111") == "(512) 555-0111" and norm("+1 512 555 0111") == "(512) 555-0111"
    assert norm("+255 712 345 678") == "+255 712 345 678"
    assert norm("0712 345 678") == "0712 345 678"
    import pytest
    with pytest.raises(ValueError):
        norm("12345")

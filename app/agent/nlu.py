"""Deterministic natural-language understanding for the receptionist.

Rules run first (fast, predictable, auditable). The LLM is only consulted as
a fallback classifier for messages the rules can't place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# ── Regexes ──────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")

RED_FLAGS = re.compile(
    r"\b(can'?t breathe|cannot breathe|difficulty breathing|trouble breathing|hard to breathe|"
    r"trouble swallowing|can'?t swallow|difficulty swallowing|won'?t stop bleeding|severe bleeding|bleeding (a lot|heavily)|"
    r"broken jaw|jaw (is )?broken|facial trauma|hit in the face|car accident|passed out|unconscious|"
    r"swelling (in|of) (my )?(eye|neck)|high fever|fever)\b",
    re.I,
)

EMERGENCY = re.compile(
    r"\b(toothache|tooth ache|dental emergency|i need (an )?emergency|this is an emergency|it'?s an emergency|"
    r"(severe|bad|terrible|unbearable|throbbing|excruciating|killing me|intense|sharp) (tooth |dental |jaw )?(pain|ache)|"
    r"(tooth|teeth|mouth|jaw|gum|gums) (really )?(hurts?|is killing|is swollen|swollen|is bleeding)|"
    r"knocked[- ]out|knocked (a|my) tooth|(broke|broken|cracked|chipped|fractured) (a |my )?(front )?(tooth|teeth|crown|filling)|"
    r"(tooth|crown|filling) (fell out|came out|broke|cracked)|lost (a |my )?(filling|crown)|abscess|swollen|swelling|"
    r"in (a lot of )?pain|pain in my (tooth|mouth|jaw)|"
    r"dolor de muela|emergencia dental|me duele (el |un )?(diente|muela|diente))\b",
    re.I,
)

INFO_ABOUT_EMERGENCY = re.compile(r"\b(do|does|can) (you|they|u)\b.*\b(emergenc|same[- ]day)", re.I)

BOOKING = re.compile(
    r"\b(book|booking|schedule|scheduling|appointment|appt|consultation|consult|come in|get (me )?in|"
    r"see (a|the) (dentist|doctor|hygienist)|make an? (appointment|visit)|set up|reserve|available times|availability|"
    r"openings|next available|first available|get started|sign me up|"
    r"cita|citas|turno|agendar|reservar)\b",
    re.I,
)
WANT = re.compile(
    r"\b(i'?d like|i would like|i want|i need|i'?m interested in|interested in|looking (for|to get)|thinking about|"
    r"considering|can i get|i wanna|quiero|quisiera|me gustar[ií]a)\b",
    re.I,
)
HOW_TO_BOOK = re.compile(r"\bhow (do|can) i (book|schedule|make)|c[oó]mo (puedo|hago) (agendar|reservar|sacar)", re.I)

QUESTION_START = re.compile(
    r"^(what|which|how|where|when|who|why|do|does|did|can|could|are|is|will|would|should|any|tell me|info|information|"
    r"qu[eé]|cu[aá]l|c[oó]mo|d[oó]nde|cu[aá]ndo|por qu[eé])\b",
    re.I,
)
GREETING = re.compile(
    r"^(hi|hello|hey|hiya|good (morning|afternoon|evening)|howdy|hola|buenos d[ií]as|buenas tardes|buenas noches)\b[\s!.,]*(there|hola)?[\s!.,]*$",
    re.I,
)
THANKS = re.compile(
    r"^("
    r"(thanks|thank you|thx|ty|gracias)([\s,!.]+(that'?s all|that is all|bye|goodbye))?|"
    r"(great|perfect|awesome|cool),? (thanks|thank you)|"
    r"(bye|goodbye)|"
    r"(that'?s|that is) (all|it)([\s,!.]+(thanks|thank you))?|"
    r"(i'?m (all )?(good|done)|nothing else|all set|nope,? that'?s all)"
    r")[\s!.]*$",
    re.I,
)
HUMAN = re.compile(
    r"\b(real person|live (agent|person|staff)|speak (to|with) (someone|a person|staff|the front desk|a human)|"
    r"talk to (someone|a person|staff|the front desk|a human|a dentist)|i'?d like to speak to a person|"
    r"call me back|hablar con (una persona|alguien|una recepcionista|el front desk))\b",
    re.I,
)
CANCEL = re.compile(
    r"\b((cancel|cancellation|call off)(\s+(my|the|this|an?))?\s+(appointment|visit|appt|booking)|cancel (it|that)|never mind.*(appointment|visit))\b",
    re.I,
)
RESCHEDULE = re.compile(
    r"\b(reschedul\w*|change (my |the )?(appointment|visit|time)|move (my |the )?(appointment|visit)|"
    r"different (time|day)|need to (move|change) it)\b",
    re.I,
)
FIND_APPOINTMENT = re.compile(
    r"\b(my appointment|when is my (appointment|visit)|find my (appointment|visit)|existing appointment|"
    r"already (have|booked) an appointment)\b",
    re.I,
)
SPANISH = re.compile(
    r"\b(hola|buenos d[ií]as|buenas tardes|buenas noches|hablas espa[nñ]ol|habla espa[nñ]ol|en espa[nñ]ol|"
    r"quiero (una )?cita|horario|seguros?|gracias|espa[nñ]ol|cita|agendar|reservar|turno|"
    r"dolor de muela|por favor|soy (paciente )?nuevo|primera vez)\b",
    re.I,
)
ENGLISH_SWITCH = re.compile(r"\b(english please|in english|speak english|en ingl[eé]s)\b", re.I)
MEMBER_SKIP = re.compile(
    r"\b(skip|later|don'?t have (it|that)|do not have it|not handy|i'?ll (bring|skip)|n/?a|"
    r"no lo (tengo|s[eé])|ahora no|lo llevo|omito)\b",
    re.I,
)
MEMBER_ID_RE = re.compile(r"\b(?:member(?:\s*id)?|id|#|n[uú]mero)\s*[:#]?\s*([A-Z0-9][-A-Z0-9]{5,18})\b", re.I)
JAILBREAK = re.compile(
    r"\b(ignore (all |the )?(previous|prior|above) (instructions|prompts)|you are now|system prompt|"
    r"reveal (your |the )?(prompt|instructions)|pretend (you are|to be)|jailbreak|developer mode)\b",
    re.I,
)
AFFIRM = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|please|absolutely|definitely|of course|let'?s do it|sounds good|y|that works|go ahead|"
    r"s[ií]|claro|vale|dale|por supuesto|est[aá] bien)\b",
    re.I,
)
DECLINE = re.compile(
    r"^(no|nope|nah|not now|not right now|not yet|maybe later|later|no thanks|no thank you|not interested|i'?ll think|i will think|just looking|just browsing|i'?m good|im good|"
    r"ahora no|m[aá]s tarde|no gracias|no por ahora)\b"
    r"|\b(not right now|maybe later|no thanks|no thank you|not interested|just (looking|browsing)|i'?ll (think about it|call later|come back)|ahora no|m[aá]s tarde)\b",
    re.I,
)
MEDICAL = re.compile(
    r"\b(should i (?!bring|pack|wear|come)|is it normal|is it safe|is it bad|"
    r"do i need (?!to bring)|what (medication|medicine|antibiotic)|antibiotics?|ibuprofen|tylenol|painkillers?|"
    r"infection|infected|diagnos\w*|symptoms?|sensitiv\w*|bleeding gums|gum disease|gingivitis|periodontitis|cavity|cavities|"
    r"wisdom (tooth|teeth)|pregnan\w*|root canal (hurt|painful)|does it hurt|is it painful|side effects?)\b",
    re.I,
)
VISIT_PREP = re.compile(
    r"\b(what should i bring|what (do i|to) bring|bring with me|do i (need to )?bring|"
    r"what to (bring|wear)|paperwork|intake forms?)\b",
    re.I,
)
MEMORY_Q = re.compile(
    r"\b("
    r"remember|what did i (ask|say|want)|what (have|did) i (ask|say)|"
    r"what (was|were) my (question|questions)|what (did|have) we (talk|chat|discuss)|"
    r"do you (still )?(know|remember)|can you (still )?remember|"
    r"recap|summar(y|ise|ize)|what was i asking"
    r")\b",
    re.I,
)
ANAPHORA = re.compile(
    r"\b("
    r"those|them|each of (them|those)|all of (them|those)|the same|"
    r"what about (that|those|them|it)|and (the )?(price|cost|that)|"
    r"how much (for|are|is) (those|them|that|it|each)"
    r")\b",
    re.I,
)
ALL_PRICES = re.compile(
    r"\b("
    r"(price|prices|pricing|cost|costs|fee|fees) of (each|every|all|the services?)|"
    r"(each|every|all) (service|treatment)s?.{0,20}\b(price|cost|fee)|"
    r"how much (is|are|for) each|"
    r"price(s)? (for|of) (all|every|each)"
    r")\b",
    re.I,
)

INSURANCE_ALIASES = {
    "Delta Dental": ["delta"],
    "Cigna": ["cigna"],
    "Aetna": ["aetna"],
    "MetLife": ["metlife", "met life"],
    "Guardian": ["guardian"],
    "United Concordia": ["concordia"],
    "Blue Cross Blue Shield": ["blue cross", "bcbs", "blue shield"],
    "Humana": ["humana"],
}
SELF_PAY = re.compile(
    r"\b(no insurance|don'?t have (dental )?insurance|do not have (dental )?insurance|self[- ]?pay|uninsured|cash|"
    r"pay out of pocket|none|sin seguro|no tengo (dental )?seguro)\b",
    re.I,
)
INSURANCE_UNSURE = re.compile(r"\b(not sure|don'?t know|unsure|no idea|i'?ll check)\b", re.I)

PATIENT_NEW = re.compile(
    r"\b(new patient|first time|first visit|never been|haven'?t been|i'?m new|im new|new|"
    r"soy (un )?paciente nuevo|primera vez|nunca he (ido|venido)|soy nuevo)\b",
    re.I,
)
PATIENT_EXISTING = re.compile(
    r"\b(existing|returning|been (there|here|in) before|current patient|already a patient|i'?m a patient|been before|visited before|"
    r"ya soy paciente|ya he (ido|venido)|soy paciente)\b",
    re.I,
)
FAMILY = re.compile(
    r"\b(family member|my (son|daughter|child|kid|kids|wife|husband|partner|mom|mother|dad|father|parent)|"
    r"mi (hijo|hija|ni[nñ]o|ni[nñ]a|esposa|esposo|marido|mam[aá]|pap[aá]|padre|madre))\b",
    re.I,
)
ANOTHER_FAMILY = re.compile(
    r"\b("
    r"book (another|a )?(family member|someone else)|another family member|"
    r"add (a )?(family member|child|kid)|also book|"
    r"book (my )?(son|daughter|child|kid|wife|husband|partner|mom|dad)"
    r")\b",
    re.I,
)

URGENCY_SOON = re.compile(
    r"\b(asap|as soon as possible|soon|urgent|urgently|this week|right away|quickly|immediately|"
    r"lo antes posible|cuanto antes|cuanto antes|urgente)\b",
    re.I,
)
URGENCY_NORMAL = re.compile(r"\b(two weeks|2 weeks|next few weeks|couple (of )?weeks|within (a|the next) (month|few weeks|2 weeks|two weeks)|next week)\b", re.I)
URGENCY_FLEX = re.compile(r"\b(flexible|no rush|whenever|any ?time|not urgent|no hurry)\b", re.I)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "1": 0,
    "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
    "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "fifth": 4, "5th": 4, "five": 4, "5": 4,
    "sixth": 5, "6th": 5, "six": 5, "6": 5,
    "last": -1,
}
NAME_PREFIX = re.compile(r"\b(?:my name is|my name'?s|name is|name:|this is|i am|i'?m|call me)\s+([A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})", re.I)
NAME_STOPWORDS = {
    "a", "an", "the", "new", "existing", "interested", "looking", "not", "just", "here", "fine", "good", "ok", "okay",
    "in", "having", "calling", "trying", "wondering", "patient", "sure", "available", "free", "flexible", "ready", "on",
}


@dataclass
class Extraction:
    email: str | None = None
    phone: str | None = None
    name: str | None = None
    service: str | None = None
    patient_type: str | None = None
    for_family: bool = False
    insurance: str | None = None
    urgency: str | None = None
    date_pref: dict | None = None
    time_pref: str | None = None
    slot_index: int | None = None
    member_id: str | None = None
    flags: dict = field(default_factory=dict)


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def title_name(raw: str) -> str:
    return " ".join(p[:1].upper() + p[1:] for p in raw.strip().split())


def detect_service(text: str, services: list[dict]) -> str | None:
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for svc in services:
        if svc["name"] == "Emergency Dentistry":
            continue  # emergencies are detected by intent, not keywords like "pain"
        for kw in [svc["name"].lower(), *svc.get("keywords", [])]:
            if re.search(rf"\b{re.escape(kw.lower())}\b", lowered):
                weight = len(kw)
                if best is None or weight > best[0]:
                    best = (weight, svc["name"])
    return best[1] if best else None


def looks_spanish(text: str) -> bool:
    return bool(SPANISH.search(text or ""))


def extract_member_id(text: str, stage: str) -> str | None:
    if not text:
        return None
    if MEMBER_SKIP.search(text):
        return "skipped"
    labeled = MEMBER_ID_RE.search(text)
    if labeled:
        return labeled.group(1).upper()
    if stage != "ask_member_id":
        return None
    if URGENCY_SOON.search(text) or URGENCY_NORMAL.search(text) or URGENCY_FLEX.search(text):
        return None
    if parse_date_pref(text, date.today()) or parse_time_pref(text):
        return None
    leftover = text.strip()
    if len(leftover.split()) > 2:
        return None
    token = re.sub(r"[^A-Za-z0-9-]", "", leftover)
    lowered = leftover.lower()
    if any(a in lowered for aliases in INSURANCE_ALIASES.values() for a in aliases):
        return None
    if 6 <= len(token) <= 18:
        return token.upper()
    return None


def detect_insurance(text: str, stage_is_insurance: bool) -> str | None:
    if THANKS.match(text.strip()) or is_question(text):
        return None
    lowered = text.lower()
    for canonical, aliases in INSURANCE_ALIASES.items():
        if any(a in lowered for a in aliases):
            return canonical
    if SELF_PAY.search(text) and (stage_is_insurance or "insurance" in lowered):
        return "Self-pay"
    if stage_is_insurance and INSURANCE_UNSURE.search(text):
        return "Not sure"
    if stage_is_insurance:
        cleaned = re.sub(r"\b(i have|we have|it'?s|its|my insurance is|insurance|through|with|work|plan)\b", "", text, flags=re.I).strip(" .,!")
        if 2 <= len(cleaned) <= 60 and not QUESTION_START.match(cleaned) and "?" not in cleaned:
            return title_name(cleaned)
    return None


def next_weekday(today: date, weekday: int) -> date:
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta)


def parse_date_pref(text: str, today: date) -> dict | None:
    t = text.lower()
    if re.search(r"\b(earliest|first available|soonest|asap|as soon as possible|any ?day|whenever|no preference|next available|anytime|any time|lo (m[aá]s )?pronto|lo antes posible)\b", t):
        return {"label": "Earliest available", "earliest": True}
    if re.search(r"\b(today|hoy)\b", t):
        return {"label": "Today", "start": today.isoformat(), "end": today.isoformat()}
    if re.search(r"\btomorrow\b", t) or re.search(r"\bma[nñ]ana\b", t) and not re.search(r"\bpor la ma[nñ]ana\b", t):
        d = today + timedelta(days=1)
        return {"label": "Tomorrow", "start": d.isoformat(), "end": d.isoformat()}
    if re.search(r"\b(next week|pr[oó]xima semana)\b", t):
        start = today + timedelta(days=7 - today.weekday())
        return {"label": "Next week", "start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat()}
    if re.search(r"\b(this week|esta semana)\b", t):
        end = today + timedelta(days=6 - today.weekday())
        if (end - today).days < 2:
            end = today + timedelta(days=4)
        return {"label": "This week", "start": today.isoformat(), "end": end.isoformat()}
    if re.search(r"\b(weekend|saturday|sat)\b", t):
        d = next_weekday(today, 5)
        return {"label": "Saturday", "start": d.isoformat(), "end": d.isoformat()}
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b({name}|{name[:3]})s?\b", t):
            d = next_weekday(today, i)
            if "next" in t and (d - today).days < 7:
                d += timedelta(days=7) if d == today or re.search(rf"next {name}", t) else timedelta()
            return {"label": name.capitalize(), "start": d.isoformat(), "end": d.isoformat()}
    m = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        month = MONTHS.index(m.group(1)) + 1
        day = int(m.group(2))
        try:
            d = date(today.year, month, day)
            if d < today:
                d = date(today.year + 1, month, day)
            return {"label": d.strftime("%b ") + str(d.day), "start": d.isoformat(), "end": d.isoformat()}
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today:
                d = date(today.year + 1, d.month, d.day)
            return {"label": d.strftime("%b ") + str(d.day), "start": d.isoformat(), "end": d.isoformat()}
        except ValueError:
            return None
    return None


def parse_time_pref(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(morning|mornings|am\b|before noon|early|por la ma[nñ]ana)\b", t):
        return "morning"
    if re.search(r"\b(evening|evenings|after work|after 3|after three|late afternoon|late|por la noche)\b", t):
        return "evening"
    if re.search(r"\b(afternoon|afternoons|lunch|pm\b|after lunch|midday|por la tarde|tardes)\b", t):
        return "afternoon"
    if re.search(r"\b(any ?time|anytime|whenever|flexible|no preference)\b", t):
        return "any"
    return None


def parse_slot_choice(text: str, offered: list[dict]) -> int | None:
    if not offered:
        return None
    t = text.lower().strip()
    m = re.search(r"\b(?:option|slot|number|#)?\s*(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|sixth|6th|last|one|two|three|four|five|six|[1-6])\b(?:\s+one)?", t)
    # Match explicit times first (e.g. "9:30", "2pm", "Tuesday at 10")
    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if tm and (tm.group(2) or tm.group(3)):
        hour = int(tm.group(1))
        minute = tm.group(2) or "00"
        suffix = (tm.group(3) or "").upper()
        candidates = []
        for i, s in enumerate(offered):
            label = s["time_label"]
            if label.startswith(f"{hour}:{minute}") and (not suffix or label.endswith(suffix)):
                candidates.append(i)
        if candidates:
            for i in candidates:
                if any(day[:3] in t for day in WEEKDAYS) and offered[i]["date_label"][:3].lower() not in t:
                    continue
                return i
    if m:
        idx = ORDINALS.get(m.group(1))
        if idx is not None and -len(offered) <= idx < len(offered):
            return idx % len(offered)
    for i, s in enumerate(offered):
        if s["label"].lower() in t:
            return i
    return None


def extract_name(text: str, stage: str) -> str | None:
    m = NAME_PREFIX.search(text)
    if m:
        cand = m.group(1).split()
        if cand and cand[0].lower() not in NAME_STOPWORDS:
            out = []  # stop at the first word that clearly isn't part of a name
            for w in cand:
                if w.lower() in NAME_STOPWORDS or w.lower() in {"and", "my", "phone", "email", "number"}:
                    break
                out.append(w)
            if out:
                return title_name(" ".join(out[:3]))
    if stage in {"ask_contact", "followup_capture"}:
        leftover = EMAIL_RE.sub(" ", text)
        leftover = PHONE_RE.sub(" ", leftover)
        leftover = re.sub(r"\b(name|phone|email|e-mail|number|is|my|and|it'?s|cell|mobile|at)\b[:]?", " ", leftover, flags=re.I)
        leftover = re.sub(r"[^A-Za-z'\-\s]", " ", leftover)
        words = [w for w in leftover.split() if w]
        if 1 <= len(words) <= 4 and all(w.lower() not in NAME_STOPWORDS for w in words):
            return title_name(" ".join(words))
    return None


def is_question(text: str) -> bool:
    return "?" in text or bool(QUESTION_START.match(text.strip()))


def extract(text: str, stage: str, services: list[dict], today: date, offered: list[dict]) -> Extraction:
    ex = Extraction()
    if not text:
        return ex
    if m := EMAIL_RE.search(text):
        ex.email = m.group(0).lower()
    text_wo_email = EMAIL_RE.sub(" ", text)
    if m := PHONE_RE.search(text_wo_email):
        ex.phone = normalize_phone(m.group(0))
    ex.name = extract_name(text, stage)
    ex.service = detect_service(text, services)
    if PATIENT_EXISTING.search(text):
        ex.patient_type = "existing"
    elif stage == "ask_patient_type" and PATIENT_NEW.search(text) or re.search(r"\b(new patient|first time|never been)\b", text, re.I):
        ex.patient_type = "new"
    if FAMILY.search(text):
        ex.for_family = True
    ex.insurance = detect_insurance(text, stage == "ask_insurance")
    if stage == "ask_urgency" or stage == "ask_preference" or stage == "ask_member_id" or URGENCY_SOON.search(text):
        if URGENCY_SOON.search(text):
            ex.urgency = "soon"
        elif URGENCY_NORMAL.search(text):
            ex.urgency = "normal"
        elif URGENCY_FLEX.search(text):
            ex.urgency = "flexible"
    if stage in {"ask_preference", "choose_slot", "ask_urgency", "ask_member_id"} or re.search(r"\b(today|tomorrow|next week|this week|morning|afternoon|evening|monday|tuesday|wednesday|thursday|friday|saturday|hoy|ma[nñ]ana|esta semana)\b", text, re.I):
        ex.date_pref = parse_date_pref(text, today)
        ex.time_pref = parse_time_pref(text)
    if stage == "choose_slot":
        ex.slot_index = parse_slot_choice(text, offered)
    ex.member_id = extract_member_id(text, stage)
    return ex


def awaited_filled(stage: str, ex: Extraction) -> bool:
    """Did the message answer the question asked at this stage?"""
    return {
        "ask_service": bool(ex.service),
        "ask_patient_type": bool(ex.patient_type or ex.for_family),
        "ask_phone": bool(ex.phone or ex.email),
        "ask_contact": bool(ex.email or ex.phone or ex.name),
        "followup_capture": bool(ex.email or ex.phone or ex.name),
        "ask_insurance": bool(ex.insurance),
        "ask_member_id": bool(ex.member_id or ex.urgency or ex.date_pref),
        "ask_urgency": bool(ex.urgency or ex.date_pref),
        "ask_preference": bool(ex.date_pref or ex.time_pref),
        "choose_slot": ex.slot_index is not None or bool(ex.date_pref or ex.time_pref),
        "find_visit": bool(ex.phone or ex.email),
    }.get(stage, False)


def classify(text: str, stage: str, ex: Extraction) -> str:
    """Rule-based intent classification. Returns one of:
    emergency, booking, info, greeting, thanks, human, affirm, decline, details,
    cancel, reschedule, find_appointment, unknown."""
    t = text.strip()
    if not t:
        return "details"
    if RED_FLAGS.search(t) or (EMERGENCY.search(t) and not INFO_ABOUT_EMERGENCY.search(t)):
        return "emergency"
    if JAILBREAK.search(t):
        return "info"
    if GREETING.match(t):
        return "greeting"
    if HUMAN.search(t):
        return "human"
    if CANCEL.search(t):
        return "cancel"
    if RESCHEDULE.search(t):
        return "reschedule"
    if FIND_APPOINTMENT.search(t) and not BOOKING.search(t):
        return "find_appointment"
    collecting = stage.startswith("ask_") or stage in {"choose_slot", "followup_capture", "find_visit", "confirm_cancel"}
    filled_something = any([ex.email, ex.phone, ex.name, ex.service, ex.patient_type, ex.insurance, ex.urgency, ex.date_pref, ex.time_pref, ex.slot_index is not None, ex.member_id])
    if THANKS.match(t):
        return "thanks"
    if stage == "ask_phone" and not (ex.phone or ex.email):
        if DECLINE.search(t):
            return "decline"
        if BOOKING.search(t) and not is_question(t):
            return "booking"
        if is_question(t):
            return "info"
        return "unknown"
    # Answering the current booking question (e.g. tapping "Invisalign") is slot-filling, not a FAQ.
    if collecting and awaited_filled(stage, ex):
        return "details"
    # A question that doesn't answer what we asked is an info request (we'll resume the flow after answering).
    if collecting and is_question(t) and not awaited_filled(stage, ex) and not BOOKING.search(t):
        return "info"
    if stage in {"offer_booking", "followup_offer"} and AFFIRM.match(t):
        return "affirm"
    if stage == "confirm_cancel" and AFFIRM.match(t):
        return "affirm"
    if stage == "confirm_cancel" and DECLINE.search(t):
        return "decline"
    if DECLINE.search(t) and not (stage == "ask_insurance" and ex.insurance) and not awaited_filled(stage, ex):
        if stage not in {"start", "booked"} or re.search(r"\b(not now|maybe later|no thanks|just looking|ahora no|m[aá]s tarde)\b", t, re.I):
            return "decline"
    if collecting and filled_something and not (is_question(t) and not ex.slot_index and not ex.date_pref):
        return "details"
    if HOW_TO_BOOK.search(t):
        return "info"
    if BOOKING.search(t) and not re.search(r"\b(cancel|reschedul)", t, re.I):
        if is_question(t) and re.search(r"\b(do you|does|how much|what|cost)\b", t, re.I) and not re.search(r"\b(can i|could i|i'?d like|i want|i need)\b", t, re.I):
            return "info"
        return "booking"
    if WANT.search(t) and ex.service:
        return "booking"
    if collecting and filled_something:
        return "details"
    if MEMORY_Q.search(t) or ALL_PRICES.search(t) or is_question(t):
        return "info"
    if ex.service:
        return "info"
    if collecting:
        return "details"
    if AFFIRM.match(t) and stage == "booked":
        return "thanks"
    return "unknown"


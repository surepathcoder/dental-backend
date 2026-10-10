"""Knowledge retrieval over the clinic's structured data (clinic profile,
services, FAQs). Hybrid BM25 + keyword scoring keeps answers grounded and
explainable without a vector database. Swap in embeddings later if the KB grows.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..knowledge_base import TEAM_SEED
from ..models import Clinic
from ..services.knowledge_docs import chunk_text, keywords_for

DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STOPWORDS = set(
    "a an the and or but if of to in on for with at by from is are was were be been do does did can could would should will "
    "you your yours we our us i me my it its this that these those what which who whom how when where why there here have has "
    "had any some about please tell know want like get".split()
)

# Colloquial / typo expansions so keyword-poor phrasing still hits the right doc.
QUERY_SYNONYMS: dict[str, list[str]] = {
    "yall": ["you", "your"],
    "ya'll": ["you"],
    "y'all": ["you"],
    "ya": ["you"],
    "take": ["accept", "insurance"],
    "takes": ["accept", "insurance"],
    "taken": ["accept"],
    "whitening": ["whitening", "whiten", "cosmetic", "zoom"],
    "whiten": ["whitening", "cosmetic"],
    "bleach": ["whitening", "cosmetic"],
    "zoom": ["whitening", "cosmetic"],
    "close": ["hours", "closing", "open"],
    "closes": ["hours", "closing"],
    "closed": ["hours", "closing"],
    "sat": ["saturday", "hours"],
    "saturday": ["saturday", "hours", "weekend"],
    "sun": ["sunday", "hours"],
    "delta": ["delta", "insurance"],
    "cigna": ["cigna", "insurance"],
    "aetna": ["aetna", "insurance"],
    "bcbs": ["blue", "cross", "insurance"],
    "cost": ["price", "pricing", "fee"],
    "costs": ["price", "pricing"],
    "price": ["pricing", "cost", "fee"],
    "pricey": ["price", "expensive"],
    "parking": ["parking", "location", "address"],
    "park": ["parking", "location"],
    "invisalign": ["invisalign", "aligners"],
    "braces": ["invisalign", "aligners", "orthodontic"],
    "aligners": ["invisalign"],
    "implant": ["implants"],
    "implants": ["implant"],
    "open": ["hours", "opening"],
    "hours": ["hours", "open", "opening"],
    "located": ["location", "address"],
    "address": ["location", "address"],
    "coverage": ["insurance", "in-network"],
    "ppo": ["insurance"],
    "horario": ["hours", "open"],
    "horarios": ["hours", "open"],
    "seguro": ["insurance"],
    "seguros": ["insurance"],
    "precio": ["price", "cost", "fee"],
    "cuesta": ["price", "cost"],
    "cuanto": ["how", "much", "price"],
    "dónde": ["where", "location"],
    "donde": ["where", "location"],
    "cita": ["book", "appointment"],
    "citas": ["book", "appointment"],
}

SERVICE_PAGE_RE = re.compile(r"/services/([a-z0-9-]+)/?$", re.I)


@dataclass
class Doc:
    id: str
    title: str
    answer: str
    keywords: list[str]
    category: str
    service: str | None = None
    meta: dict = field(default_factory=dict)


def _fmt_time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h = h % 12 or 12
    return f"{h}:{m:02d} {suffix}" if m else f"{h} {suffix}"


def hours_sentence(hours: dict) -> str:
    parts = []
    for key, name in DAY_NAMES.items():
        span = hours.get(key)
        parts.append(f"{name} {_fmt_time(span['open'])}–{_fmt_time(span['close'])}" if span else f"{name} closed")
    return "; ".join(parts)


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOPWORDS and len(w) > 1}


def token_list(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOPWORDS and len(w) > 1]


def expand_query(query: str) -> str:
    """Rewrite colloquial phrasing so BM25/keywords can match structured docs."""
    words = re.findall(r"[a-z0-9']+", query.lower())
    extra: list[str] = []
    for w in words:
        extra.extend(QUERY_SYNONYMS.get(w, []))
    if extra:
        return query + " " + " ".join(extra)
    return query


def service_slug_from_page(page: str | None) -> str | None:
    if not page:
        return None
    m = SERVICE_PAGE_RE.search(page.strip())
    return m.group(1).lower() if m else None


def is_open_now(clinic: Clinic, now: datetime | None = None) -> bool:
    tz = ZoneInfo(clinic.timezone or "America/Chicago")
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    span = (clinic.hours or {}).get(DAY_KEYS[now.weekday()])
    if not span:
        return False
    open_t = time.fromisoformat(span["open"])
    close_t = time.fromisoformat(span["close"])
    t = now.timetz().replace(tzinfo=None)
    return open_t <= t < close_t


def next_open_label(clinic: Clinic, now: datetime | None = None) -> str:
    tz = ZoneInfo(clinic.timezone or "America/Chicago")
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    for offset in range(0, 8):
        day = now.date() + timedelta(days=offset)
        span = (clinic.hours or {}).get(DAY_KEYS[day.weekday()])
        if not span:
            continue
        open_t = time.fromisoformat(span["open"])
        if offset == 0 and now.timetz().replace(tzinfo=None) >= open_t:
            continue
        label = "today" if offset == 0 else "tomorrow" if offset == 1 else DAY_NAMES[DAY_KEYS[day.weekday()]]
        return f"{label} at {_fmt_time(span['open'])}"
    return "our next business day"


def after_hours_note(clinic: Clinic, now: datetime | None = None) -> str | None:
    if is_open_now(clinic, now):
        return None
    nxt = next_open_label(clinic, now)
    return (
        f"We're currently closed — we open {nxt}. I can still answer questions and book a visit, "
        f"and for a dental emergency we keep same-day time on weekdays. After hours you can also call {clinic.phone}."
    )


def is_demo_clinic(clinic: Clinic) -> bool:
    """A clinic made for one prospect's demo link — it holds only that business's public facts."""
    return bool((clinic.settings or {}).get("demo"))


def build_docs(clinic: Clinic) -> list[Doc]:
    services = [s for s in clinic.services if s.is_active]
    insurers = list(clinic.insurance_accepted or [])
    if not insurers:
        insurance_answer = (
            "We have not published an in-network list yet. Call the front desk and we will check your benefits before your visit."
        )
    elif len(insurers) == 1:
        insurance_answer = (
            f"Yes — we're in-network with {insurers[0]}. We verify your benefits before your visit and file claims for you."
        )
    else:
        insurance_answer = (
            "Yes — we're in-network with most major PPO plans, including "
            + ", ".join(insurers[:-1])
            + f" and {insurers[-1]}. We verify your benefits before your visit and file claims for you."
        )
    if services:
        if len(services) == 1:
            services_answer = f"We offer {services[0].name}."
        else:
            services_answer = (
                "We offer "
                + ", ".join(s.name for s in services[:-1])
                + f" and {services[-1].name}."
            )
    else:
        services_answer = "Ask the front desk which treatments we currently offer."
    docs: list[Doc] = [
        Doc(
            "hours",
            "Opening hours",
            f"Our hours are: {hours_sentence(clinic.hours or {})}."
            + ("" if is_demo_clinic(clinic) else " Emergency appointments are reserved every weekday."),
            ["hours", "open", "opening", "close", "closing", "saturday", "sunday", "weekend", "today", "time"],
            "Hours & Location",
        ),
        Doc(
            "location",
            "Location",
            f"We're located at {clinic.address_line}, {clinic.city}, {clinic.state} {clinic.zip_code}, with free covered parking in front of the building.",
            ["where", "location", "located", "address", "directions", "parking", "park", "find you"],
            "Hours & Location",
        ),
        Doc(
            "contact",
            "Contact",
            f"You can reach our front desk at {clinic.phone} or {clinic.email}.",
            ["phone", "call", "number", "email", "contact", "reach"],
            "General Question",
        ),
        Doc(
            "insurance",
            "Insurance",
            insurance_answer,
            ["insurance", "ppo", "in-network", "network", "coverage", "covered", "accept", "take", "plan", "plans"]
            + [i.lower() for i in insurers]
            + ["delta", "bcbs", "blue cross", "met life"],
            "Insurance Question",
            meta={"insurers": insurers},
        ),
        Doc(
            "services",
            "Services overview",
            services_answer,
            ["services", "offer", "treatments", "procedures", "do you do", "what do you", "provide", "specialize"],
            "Services Question",
        ),
        Doc(
            "team",
            "Our dentists",
            (f"Our team at {clinic.name} will be happy to introduce themselves when you visit — call {clinic.phone} "
             "to ask about a particular dentist." if is_demo_clinic(clinic) else
             "Our team includes " + ", ".join(f"{m['name']} ({m['role']})" for m in TEAM_SEED) + "."),
            ["dentist", "dentists", "doctor", "doctors", "team", "who", "staff", "hygienist"],
            "General Question",
        ),
    ]
    if clinic.financing:
        docs.append(
            Doc(
                "financing",
                "Financing",
                f"{clinic.financing.get('summary', '')} {clinic.financing.get('membership_plan', '')}".strip(),
                ["financing", "finance", "payment plan", "payments", "monthly", "carecredit", "sunbit", "membership", "afford", "uninsured", "no insurance"],
                "Pricing Question",
            )
        )
    for s in services:
        docs.append(
            Doc(
                f"service:{s.slug}",
                s.name,
                f"Yes — we offer {s.name.lower() if s.name != 'Invisalign' else 'Invisalign'} at {clinic.name}. {s.description}".strip(),
                [s.name.lower(), *s.keywords],
                "Services Question",
                service=s.name,
                meta={"slug": s.slug, "duration": s.duration_minutes, "price_note": s.price_note},
            )
        )
    for f in clinic.faqs:
        docs.append(Doc(f"faq:{f.id}", f.question, f.answer, f.keywords or [], {
            "practice": "Hours & Location", "insurance": "Insurance Question", "pricing": "Pricing Question",
            "emergency": "Emergency Question", "booking": "Booking Question",
        }.get(f.category, "General Question")))
    for doc in getattr(clinic, "knowledge_documents", None) or []:
        if not doc.is_active or not (doc.text or "").strip():
            continue
        chunks = chunk_text(doc.text)
        base_kw = list(doc.keywords or []) or keywords_for(doc.title, doc.text)
        for i, chunk in enumerate(chunks):
            suffix = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            docs.append(
                Doc(
                    f"doc:{doc.id}:{i}",
                    f"{doc.title}{suffix}",
                    chunk,
                    keywords_for(doc.title, chunk, base_kw),
                    "Practice knowledge",
                    meta={"document_id": doc.id, "filename": doc.filename},
                )
            )
    return docs


def retrieve(query: str, docs: list[Doc], k: int = 3) -> list[tuple[Doc, float]]:
    expanded = expand_query(query)
    q = expanded.lower()
    q_tokens = tokens(expanded)
    if not q_tokens and not q.strip():
        return []

    corpuses: list[list[str]] = []
    for d in docs:
        text = f"{d.title} {d.answer} {' '.join(d.keywords)}"
        corpuses.append(token_list(text) or token_list(d.title))

    n_docs = len(docs) or 1
    avgdl = sum(len(c) for c in corpuses) / n_docs
    df: Counter[str] = Counter()
    for c in corpuses:
        df.update(set(c))

    k1, b = 1.5, 0.75
    scored: list[tuple[Doc, float]] = []
    for d, toks in zip(docs, corpuses):
        tf = Counter(toks)
        dl = len(toks) or 1
        bm25 = 0.0
        for qi in q_tokens:
            n_qi = df.get(qi, 0)
            if not n_qi:
                continue
            idf = math.log((n_docs - n_qi + 0.5) / (n_qi + 0.5) + 1.0)
            f = tf[qi]
            bm25 += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))

        kw_boost = 0.0
        for kw in d.keywords:
            kw_l = kw.lower()
            if kw_l in STOPWORDS:
                continue
            if re.search(rf"\b{re.escape(kw_l)}\b", q):
                kw_boost += 2.0 if " " in kw_l else 1.0
                if d.service and kw_l not in {"cost", "price", "prices", "pricing", "fee", "fees", "emergency", "urgent"}:
                    kw_boost += 2.0
        kw_boost += 0.5 * len(q_tokens & tokens(d.title))
        if d.service and re.search(rf"\b{re.escape(d.service.lower())}\b", q):
            kw_boost += 1.5

        score = bm25 + kw_boost
        # Require a keyword hit so incidental token overlap cannot retrieve structured FAQs.
        # Uploaded practice docs may match on BM25 alone (keywords are derived from the file).
        uploaded = d.id.startswith("doc:")
        if kw_boost >= 1.0 or (uploaded and bm25 >= 1.5):
            scored.append((d, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def insurance_specific_answer(query: str, insurers: list[str]) -> str | None:
    """Precise answer for 'Do you take <insurer>?' questions."""
    q = query.lower()
    aliases = {"bcbs": "Blue Cross Blue Shield", "blue cross": "Blue Cross Blue Shield", "delta": "Delta Dental", "met life": "MetLife", "concordia": "United Concordia"}
    matches = [i for i in insurers if i.lower() in q] or [ins for alias, ins in aliases.items() if alias in q and ins in insurers]
    if matches:
        return f"Yes — we're in-network with {matches[0]}. We'll verify your benefits before your visit and file the claim for you."
    return None


def doc_for_service_slug(docs: list[Doc], slug: str | None) -> Doc | None:
    if not slug:
        return None
    return next((d for d in docs if d.meta.get("slug") == slug), None)


def service_price_catalog(docs: list[Doc]) -> Doc | None:
    lines: list[str] = []
    seen: set[str] = set()
    for d in docs:
        name = d.service
        note = (d.meta or {}).get("price_note")
        if not name or not note or name in seen:
            continue
        seen.add(name)
        lines.append(f"{name}: {note}")
    if not lines:
        return None
    answer = (
        "Here's a starting range for each service we list. Consultations for cosmetic, implant and Invisalign are complimentary; "
        "treatment is quoted after an exam. " + " ".join(lines)
    )
    return Doc(
        "catalog:prices",
        "Price of each service",
        answer,
        ["price", "prices", "cost", "each", "every", "service", "services", "fee"],
        "Pricing Question",
    )

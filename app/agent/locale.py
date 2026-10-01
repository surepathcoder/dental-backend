"""Spanish vs English copy for the receptionist. Locale is stored on the
conversation (`fields["locale"]`) once Spanish is detected so the rest of
the visit stays in the patient's language even if they tap an English chip.
"""

from __future__ import annotations

from typing import Any

START_REPLIES_ES = ["Reservar una cita", "¿Aceptan mi seguro?", "Invisalign", "Emergencia dental"]
PATIENT_REPLIES_ES = ["Soy paciente nuevo", "Ya soy paciente", "Es para un familiar"]
INSURANCE_REPLIES_ES = ["Delta Dental", "Cigna", "Aetna", "MetLife", "Blue Cross Blue Shield", "Sin seguro", "No estoy seguro"]
URGENCY_REPLIES_ES = ["Lo antes posible", "En las próximas 2 semanas", "Soy flexible"]
PREFERENCE_REPLIES_ES = ["Lo más pronto", "Esta semana", "La próxima semana", "Por la mañana", "Por la tarde", "Sábado"]
OFFER_TEXT_ES = {
    "Invisalign": "¿Quiere agendar una consulta gratuita de Invisalign?",
    "Dental Implants": "¿Quiere agendar una consulta gratuita de implantes?",
    "Cosmetic Dentistry": "¿Quiere agendar una consulta cosmética gratuita?",
    "General Dentistry": "¿Quiere agendar un examen con uno de nuestros dentistas?",
    "Emergency Dentistry": "¿Quiere que le busque una cita de emergencia?",
}


def is_es(fields: dict[str, Any] | None) -> bool:
    return (fields or {}).get("locale") == "es"


def t(fields: dict[str, Any] | None, en: str, es: str) -> str:
    return es if is_es(fields) else en


def start_replies(fields: dict[str, Any] | None) -> list[str]:
    return START_REPLIES_ES if is_es(fields) else ["Book an appointment", "Do you accept my insurance?", "Tell me about Invisalign", "I have a dental emergency"]


def patient_replies(fields: dict[str, Any] | None) -> list[str]:
    return PATIENT_REPLIES_ES if is_es(fields) else ["I'm a new patient", "I'm an existing patient", "It's for a family member"]


def insurance_replies(fields: dict[str, Any] | None) -> list[str]:
    return INSURANCE_REPLIES_ES if is_es(fields) else ["Delta Dental", "Cigna", "Aetna", "MetLife", "Blue Cross Blue Shield", "No insurance (self-pay)", "Not sure"]


def urgency_replies(fields: dict[str, Any] | None) -> list[str]:
    return URGENCY_REPLIES_ES if is_es(fields) else ["As soon as possible", "Within the next 2 weeks", "I'm flexible"]


def preference_replies(fields: dict[str, Any] | None) -> list[str]:
    return PREFERENCE_REPLIES_ES if is_es(fields) else ["Earliest available", "This week", "Next week", "Mornings", "Afternoons", "Saturday"]


def offer_text(fields: dict[str, Any] | None, service: str, english: dict[str, str]) -> str:
    if is_es(fields):
        return OFFER_TEXT_ES.get(service, f"¿Quiere agendar una cita de {service.lower()}?")
    return english.get(service, f"Would you like to schedule a {service.lower()} appointment?")


def spanish_greeting(phone: str) -> str:
    return (
        f"¡Hola! Sí, nuestro equipo habla español. Puedo ayudarle a reservar una cita aquí mismo. "
        f"Si prefiere hablar con una persona, llame al {phone}."
    )

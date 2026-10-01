"""Structured clinic knowledge used to seed the database and ground the AI.

BrightSmile Dental is a FICTIONAL practice. Phone numbers use the reserved
555-01xx range and e-mail addresses use the reserved ``.example`` TLD.
"""

from __future__ import annotations

CLINIC_SEED = {
    "name": "BrightSmile Dental",
    "tagline": "Modern dental care. Made simple.",
    "phone": "(512) 555-0142",
    "email": "hello@brightsmile.example",
    "address_line": "4120 Bright Leaf Blvd, Suite 200",
    "city": "Austin",
    "state": "TX",
    "zip_code": "78731",
    "timezone": "America/Chicago",
    "hours": {
        "mon": {"open": "08:00", "close": "17:00"},
        "tue": {"open": "08:00", "close": "17:00"},
        "wed": {"open": "08:00", "close": "18:00"},
        "thu": {"open": "08:00", "close": "17:00"},
        "fri": {"open": "08:00", "close": "15:00"},
        "sat": {"open": "09:00", "close": "13:00"},
        "sun": None,
    },
    "insurance_accepted": [
        "Delta Dental",
        "Cigna",
        "Aetna",
        "MetLife",
        "Guardian",
        "United Concordia",
        "Blue Cross Blue Shield",
        "Humana",
    ],
    "financing": {
        "providers": ["CareCredit", "Sunbit"],
        "membership_plan": "BrightSmile Membership — $29/month for uninsured patients: two cleanings, exams, X-rays and 15% off treatment.",
        "summary": "Flexible monthly payment options through CareCredit and Sunbit, plus an in-house membership plan for patients without insurance.",
    },
    "settings": {
        "assistant": {
            "name": "Ava",
            "greeting": "Hi! I'm Ava, BrightSmile Dental's AI receptionist. I can answer questions about our services, insurance and hours — or help you book a visit. How can I help today?",
            "tone": "warm",
            "handoff_phone": "(512) 555-0142",
            "collect_insurance": True,
            "collect_urgency": True,
        },
        "appointments": {
            "slot_interval_minutes": 30,
            "min_notice_hours": 2,
            "booking_window_days": 21,
            "slots_to_offer": 6,
            "emergency_same_day": True,
        },
        "sms": {
            "confirmation_template": "Hi {first_name}! Your {service} visit at {clinic} is confirmed for {date} at {time}. Need to change it? {booking_url} or call {phone}.",
            "reminder_template": "Reminder: {first_name}, your {clinic} appointment is {when}. Reschedule: {booking_url}",
        },
        "email": {
            "from_name": "BrightSmile Dental",
            "confirmation_subject": "Your appointment is confirmed",
            "reminder_subject": "Reminder: your appointment",
        },
        "brand": {
            "google_review_url": "",
            "production_per_visit": 250,
            "primary_color": "#0f6b70",
        },
    },
}

SERVICES_SEED = [
    {
        "slug": "general-dentistry",
        "name": "General Dentistry",
        "category": "general",
        "short_description": "Exams, cleanings, X-rays, fillings and preventive care for the whole family.",
        "description": "Our general dentistry visits include a comprehensive exam, professional cleaning, digital X-rays when needed, oral cancer screening and a personalized prevention plan. We also provide tooth-colored fillings, crowns, root canal therapy and night guards.",
        "duration_minutes": 60,
        "price_from": 129,
        "price_note": "New patient exam, cleaning and X-rays are typically covered at 100% by in-network PPO plans. Self-pay new patient special: $129.",
        "keywords": ["cleaning", "checkup", "check-up", "exam", "x-ray", "xray", "filling", "fillings", "crown", "crowns", "root canal", "preventive", "hygiene", "family", "kids", "children", "general"],
        "sort_order": 1,
    },
    {
        "slug": "cosmetic-dentistry",
        "name": "Cosmetic Dentistry",
        "category": "cosmetic",
        "short_description": "Professional whitening, porcelain veneers, bonding and smile makeovers.",
        "description": "Cosmetic treatment starts with a smile consultation where we discuss your goals and review options such as professional teeth whitening, porcelain veneers, cosmetic bonding and complete smile makeovers.",
        "duration_minutes": 45,
        "price_from": 349,
        "price_note": "In-office whitening from $349. Veneers are quoted per tooth after a consultation. Cosmetic consultations are complimentary.",
        "keywords": ["cosmetic", "whitening", "whiten", "white", "veneer", "veneers", "bonding", "smile makeover", "makeover", "stain", "stains", "yellow"],
        "sort_order": 2,
    },
    {
        "slug": "dental-implants",
        "name": "Dental Implants",
        "category": "restorative",
        "short_description": "Permanent, natural-looking replacements for missing teeth — from single implants to full-arch.",
        "description": "Dental implants replace missing teeth with a titanium post and a custom crown. Your implant consultation includes a 3D CBCT scan, an exam with our implant dentist, and a written treatment plan with transparent pricing and financing options.",
        "duration_minutes": 60,
        "price_from": 3200,
        "price_note": "Single implants (post, abutment and crown) typically start around $3,200. Your exact quote is provided after the consultation and 3D scan.",
        "keywords": ["implant", "implants", "missing tooth", "missing teeth", "replace tooth", "dentures", "denture", "all-on-4", "full arch", "bridge"],
        "sort_order": 3,
    },
    {
        "slug": "invisalign",
        "name": "Invisalign",
        "category": "orthodontics",
        "short_description": "Clear aligners to straighten your teeth discreetly — with a free 3D smile scan.",
        "description": "Invisalign straightens teeth with a series of custom clear aligners. Your consultation includes a digital iTero 3D scan, a preview of your projected results, and an estimated treatment timeline.",
        "duration_minutes": 45,
        "price_from": 3500,
        "price_note": "Invisalign treatment typically ranges from $3,500 to $6,500 depending on complexity. Consultations and 3D scans are complimentary. Many PPO plans include orthodontic benefits.",
        "keywords": ["invisalign", "aligner", "aligners", "clear aligners", "braces", "straighten", "straight", "crooked", "orthodontic", "orthodontics", "gap", "crowding"],
        "sort_order": 4,
    },
    {
        "slug": "emergency-dentistry",
        "name": "Emergency Dentistry",
        "category": "emergency",
        "short_description": "Same-day appointments for toothaches, broken teeth, swelling and dental injuries.",
        "description": "We reserve time every weekday for dental emergencies such as severe toothaches, cracked or knocked-out teeth, lost fillings or crowns, and swelling. Call us as early as possible and we will do our best to see you the same day.",
        "duration_minutes": 30,
        "price_from": 95,
        "price_note": "Emergency exams with a focused X-ray start at $95 for self-pay patients and are often covered by insurance.",
        "keywords": ["emergency", "urgent", "toothache", "pain", "broken", "cracked", "chipped", "knocked out", "swelling", "swollen", "abscess", "lost filling", "same day", "same-day"],
        "sort_order": 5,
    },
]

FAQS_SEED = [
    {
        "question": "What are your opening hours?",
        "answer": "We're open Monday, Tuesday and Thursday 8:00 AM–5:00 PM, Wednesday 8:00 AM–6:00 PM, Friday 8:00 AM–3:00 PM and Saturday 9:00 AM–1:00 PM. We're closed on Sundays.",
        "category": "practice",
        "keywords": ["hours", "open", "opening", "close", "closing", "saturday", "sunday", "weekend", "time", "when"],
    },
    {
        "question": "Where are you located?",
        "answer": "We're at 4120 Bright Leaf Blvd, Suite 200, Austin, TX 78731 — in Northwest Hills, about 10 minutes from downtown. There's free covered parking in front of the building.",
        "category": "practice",
        "keywords": ["where", "location", "located", "address", "directions", "parking", "park", "austin"],
    },
    {
        "question": "Do you accept insurance?",
        "answer": "Yes. We're in-network with most major PPO plans, including Delta Dental, Cigna, Aetna, MetLife, Guardian, United Concordia, Blue Cross Blue Shield and Humana. We'll verify your benefits before your visit and file claims for you.",
        "category": "insurance",
        "keywords": ["insurance", "ppo", "in-network", "network", "delta", "cigna", "aetna", "metlife", "guardian", "concordia", "blue cross", "bcbs", "humana", "coverage", "covered", "accept", "take"],
    },
    {
        "question": "Do you offer financing?",
        "answer": "Yes. We offer flexible monthly payments through CareCredit and Sunbit, and our BrightSmile Membership ($29/month) covers two cleanings, exams and X-rays per year plus 15% off treatment for patients without insurance.",
        "category": "insurance",
        "keywords": ["financing", "finance", "payment plan", "payments", "monthly", "carecredit", "sunbit", "membership", "afford", "no insurance", "uninsured"],
    },
    {
        "question": "How much does an appointment cost?",
        "answer": "For new patients, an exam, cleaning and X-rays are usually covered at 100% by in-network PPO insurance. Without insurance, our new patient special is $129. Emergency exams start at $95, and cosmetic, implant and Invisalign consultations are complimentary. Treatment costs are always explained before any work begins.",
        "category": "pricing",
        "keywords": ["cost", "price", "prices", "pricing", "how much", "fee", "fees", "expensive", "cheap", "afford", "charge"],
    },
    {
        "question": "Do you offer emergency appointments?",
        "answer": "Yes. We reserve same-day appointments every weekday for dental emergencies such as severe toothaches, broken or knocked-out teeth and swelling. The earlier you reach out, the better the chance we can see you today.",
        "category": "emergency",
        "keywords": ["emergency", "same day", "same-day", "urgent", "today", "asap"],
    },
    {
        "question": "How do I book an appointment?",
        "answer": "You can book right here in this chat, use the online booking page, or call us at (512) 555-0142. I just need a few details — the service you're interested in, your contact information and a preferred time.",
        "category": "booking",
        "keywords": ["book", "booking", "schedule", "appointment", "make an appointment", "how do i"],
    },
    {
        "question": "What should I expect at my first visit?",
        "answer": "Your first visit takes about 60 minutes and includes digital X-rays, a comprehensive exam, a cleaning if appropriate, and time to talk through any questions. Please bring your ID and insurance card. You'll receive digital intake forms by email before your visit.",
        "category": "new_patients",
        "keywords": ["first visit", "new patient", "expect", "bring", "forms", "paperwork", "first appointment"],
    },
    {
        "question": "Do you see children?",
        "answer": "Yes — we're a family practice and see children from age 3, as well as teens and adults. Many families book back-to-back appointments.",
        "category": "general",
        "keywords": ["children", "kids", "child", "son", "daughter", "family", "pediatric", "teen"],
    },
    {
        "question": "Do you offer sedation for anxious patients?",
        "answer": "We offer a calm, judgment-free environment and comfort options such as noise-cancelling headphones, blankets and nitrous oxide (laughing gas) for eligible patients. Your dentist will discuss which options are appropriate for you.",
        "category": "comfort",
        "keywords": ["anxious", "anxiety", "nervous", "scared", "fear", "sedation", "nitrous", "laughing gas", "comfort"],
    },
    {
        "question": "What is your cancellation policy?",
        "answer": "We kindly ask for 24 hours' notice if you need to cancel or reschedule so we can offer the time to another patient. You can reply to your reminder text or call us.",
        "category": "booking",
        "keywords": ["cancel", "cancellation", "reschedule", "change appointment", "move appointment"],
    },
    {
        "question": "What languages do you speak?",
        "answer": "Our team speaks English and Spanish. Hablamos español.",
        "category": "practice",
        "keywords": ["spanish", "espanol", "español", "language", "languages"],
    },
]

TEAM_SEED = [
    {"name": "Dr. Emily Carter, DDS", "role": "Founder & General Dentist"},
    {"name": "Dr. Marcus Reed, DMD", "role": "Implant & Restorative Dentist"},
    {"name": "Dr. Priya Nair, DDS", "role": "Cosmetic Dentist & Invisalign Provider"},
    {"name": "Daniel Ortiz, RDH", "role": "Lead Dental Hygienist"},
]

STAFF_USERS_SEED = [
    {"name": "Dr. Emily Carter", "email": "emily.carter@brightsmile.example", "role": "owner"},
    {"name": "Hannah Brooks", "email": "front.desk@brightsmile.example", "role": "front_desk"},
]

AUTOMATIONS_SEED = [
    {
        "key": "ai_receptionist",
        "name": "AI Receptionist",
        "description": "Answers patient questions 24/7 from the clinic knowledge base and routes booking and emergency requests.",
        "trigger": "Website visitor opens chat",
        "steps": [
            {"type": "trigger", "label": "Visitor opens chat"},
            {"type": "ai", "label": "Detect intent"},
            {"type": "action", "label": "Answer from knowledge base"},
            {"type": "result", "label": "Offer appointment"},
        ],
        "config": {"after_hours_only": False},
    },
    {
        "key": "lead_qualification",
        "name": "Lead Qualification",
        "description": "Collects contact details, treatment interest, insurance and urgency, then scores the lead HOT / WARM / COLD.",
        "trigger": "Appointment intent detected",
        "steps": [
            {"type": "trigger", "label": "New lead"},
            {"type": "ai", "label": "AI qualification"},
            {"type": "action", "label": "Lead saved"},
            {"type": "action", "label": "Appointment offered"},
            {"type": "result", "label": "Appointment booked"},
        ],
        "config": {"required_fields": ["name", "phone", "email", "service"]},
    },
    {
        "key": "appointment_confirmation",
        "name": "Appointment Confirmation",
        "description": "Sends an email and SMS confirmation the moment an appointment is booked.",
        "trigger": "Appointment booked",
        "steps": [
            {"type": "trigger", "label": "Appointment booked"},
            {"type": "action", "label": "Send confirmation email"},
            {"type": "action", "label": "Send confirmation SMS"},
            {"type": "result", "label": "Patient confirmed"},
        ],
        "config": {"channels": ["email", "sms"]},
    },
    {
        "key": "sms_reminder",
        "name": "SMS Reminders",
        "description": "Reduces no-shows with automatic reminders 24 hours, 2 hours, and 30 minutes before each appointment.",
        "trigger": "Appointment scheduled",
        "steps": [
            {"type": "trigger", "label": "Appointment scheduled"},
            {"type": "delay", "label": "24h before"},
            {"type": "action", "label": "Send reminder SMS"},
            {"type": "delay", "label": "2h before"},
            {"type": "delay", "label": "30m before"},
            {"type": "result", "label": "Final reminder sent"},
        ],
        "config": {"reminder_hours": [24, 2, 0.5]},
    },
    {
        "key": "missed_lead_followup",
        "name": "Missed Lead Follow-up",
        "description": "When a visitor starts booking and leaves without finishing, or shows interest without booking, schedules a friendly follow-up message.",
        "trigger": "Interested visitor leaves without booking",
        "steps": [
            {"type": "trigger", "label": "Chat ended, not booked"},
            {"type": "action", "label": "Lead marked FOLLOW_UP"},
            {"type": "delay", "label": "Wait 24 hours"},
            {"type": "action", "label": "Send follow-up SMS / email"},
            {"type": "result", "label": "Lead re-engaged"},
        ],
        "config": {
            "delay_hours": 24,
            "cadence_hours": [24, 72],
            "template": "Hi {first_name}, this is {clinic}. You were interested in {service_lower}. Would you like help finding a time? Book here: {booking_url}",
        },
    },
    {
        "key": "review_request",
        "name": "Review Request",
        "description": "Asks happy patients for a Google review a few hours after their visit is marked completed.",
        "trigger": "Appointment completed",
        "steps": [
            {"type": "trigger", "label": "Appointment completed"},
            {"type": "delay", "label": "Wait 3 hours"},
            {"type": "action", "label": "Send review request"},
            {"type": "result", "label": "Review collected"},
        ],
        "config": {"delay_hours": 3},
    },
    {
        "key": "no_show_recovery",
        "name": "No-show recovery",
        "description": "Texts the patient the same day when a visit is marked no-show and offers a new booking link.",
        "trigger": "Appointment marked NO_SHOW",
        "steps": [
            {"type": "trigger", "label": "No-show recorded"},
            {"type": "action", "label": "Send recovery message"},
            {"type": "result", "label": "Patient rebooked or closed"},
        ],
        "config": {"delay_hours": 1},
    },
    {
        "key": "hygiene_recall",
        "name": "Hygiene recall",
        "description": "Reminds patients they are due for a cleaning about six months after a general dentistry visit.",
        "trigger": "General dentistry appointment booked",
        "steps": [
            {"type": "trigger", "label": "Cleaning booked"},
            {"type": "delay", "label": "Wait 6 months"},
            {"type": "action", "label": "Send recall message"},
            {"type": "result", "label": "Hygiene schedule filled"},
        ],
        "config": {"delay_days": 180},
    },
    {
        "key": "staff_handoff_alert",
        "name": "Front desk alert",
        "description": "Emails the practice when a visitor asks to speak with a person so the chat is not missed.",
        "trigger": "Visitor asks for a human",
        "steps": [
            {"type": "trigger", "label": "Talk to a person"},
            {"type": "action", "label": "Email front desk"},
            {"type": "result", "label": "Staff joins the chat"},
        ],
        "config": {},
    },
    {
        "key": "treatment_plan_followup",
        "name": "Treatment-plan follow-up",
        "description": "After an Invisalign, implant or cosmetic consult is marked complete, nudges the patient at 3 days and 10 days to schedule treatment.",
        "trigger": "High-value consult completed",
        "steps": [
            {"type": "trigger", "label": "Consult marked completed"},
            {"type": "delay", "label": "Wait 3 days"},
            {"type": "action", "label": "Send treatment nudge"},
            {"type": "delay", "label": "Wait 10 days"},
            {"type": "action", "label": "Send second nudge"},
            {"type": "result", "label": "Case scheduled or closed"},
        ],
        "config": {"cadence_days": [3, 10]},
    },
    {
        "key": "verify_benefits",
        "name": "Verify insurance benefits",
        "description": "When a booked patient shares a plan (and optionally a member ID), queues a front-desk task to verify benefits before the visit.",
        "trigger": "Appointment booked with insurance",
        "steps": [
            {"type": "trigger", "label": "Insurance captured in chat"},
            {"type": "action", "label": "Queue verify-benefits task"},
            {"type": "result", "label": "Desk confirms coverage"},
        ],
        "config": {},
    },
]

INTEGRATIONS_SEED = ["groq", "openai", "twilio", "gmail", "resend", "calcom", "posthog"]

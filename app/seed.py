"""Demo dataset: one persona per scenario.

This is the single source of truth. `scripts/seed_sheet.py` pushes it to Google
Sheets (or renders CSVs), and `MemoryRepository` serves it directly so the agent
is runnable (and testable) without any credentials.

Note the phone numbers: no digit zero anywhere. The first set used the reserved
fictional 555-01xx range, and the first live call failed four times on it —
"five five five, oh one four" is read back as "0 1 4" or "4 1 4" depending on
whether the speaker says "oh" or "zero", and a dropped leading zero turns a
10-digit number into 9. Test data for a voice agent has to be *speakable*: no
zeros, distinct leading triples, and doubled digits in the last four to give
the caller a natural rhythm. Same reason the DOBs avoid ambiguous month/day
pairs like 03/04.
"""

from __future__ import annotations

# Scenario map:
#   555-234-1187  Daniel Okafor   -> happy path, claim in review
#   555-361-2234  Priya Raman     -> claim needs documents
#   555-418-4492  Marcus Webb     -> authentication failure (give a wrong DOB)
#   555-572-5513  Lena Fischer    -> authenticated, but has no open claims
#   555-693-6678  Ana Duarte      -> denied claim, asks for a representative
#   555-999-8888  (nobody)        -> customer not found

CUSTOMERS: list[dict[str, str]] = [
    {
        "phone": "555-234-1187",
        "full_name": "Daniel Okafor",
        "preferred_name": "Daniel",
        "date_of_birth": "1979-11-03",
        "policy_id": "POL-11204",
        "email": "daniel.okafor@example.com",
    },
    {
        "phone": "555-361-2234",
        "full_name": "Priya Raman",
        "preferred_name": "Priya",
        "date_of_birth": "1988-04-12",
        "policy_id": "POL-11288",
        "email": "priya.raman@example.com",
    },
    {
        "phone": "555-418-4492",
        "full_name": "Marcus Webb",
        "preferred_name": "Marcus",
        "date_of_birth": "1965-02-28",
        "policy_id": "POL-11310",
        "email": "marcus.webb@example.com",
    },
    {
        "phone": "555-572-5513",
        "full_name": "Lena Fischer",
        "preferred_name": "Lena",
        "date_of_birth": "1993-07-21",
        "policy_id": "POL-11355",
        "email": "lena.fischer@example.com",
    },
    {
        "phone": "555-693-6678",
        "full_name": "Ana Duarte",
        "preferred_name": "Ana",
        "date_of_birth": "1982-01-09",
        "policy_id": "POL-11402",
        "email": "ana.duarte@example.com",
    },
]

CLAIMS: list[dict[str, str]] = [
    {
        "claim_id": "CLM-48201",
        "phone": "555-234-1187",
        "claim_type": "Auto collision",
        "status": "In Review",
        "status_detail": "An adjuster has been assigned and your photos were accepted.",
        "amount": "",
        "adjuster_name": "Rosa Lindqvist",
        "required_documents": "",
        "submission_deadline": "",
        "last_updated": "2026-10-13",
        "next_step": "Your adjuster inspection is scheduled for October 20th.",
    },
    {
        "claim_id": "CLM-48255",
        "phone": "555-361-2234",
        "claim_type": "Home water damage",
        "status": "Documents Required",
        "status_detail": "We cannot continue the review until two documents are received.",
        "amount": "",
        "adjuster_name": "Tomas Reyes",
        "required_documents": "Photos of the water damage; the plumber's itemised invoice",
        "submission_deadline": "2026-10-29",
        "last_updated": "2026-10-11",
        "next_step": "Upload the documents, then review resumes within two business days.",
    },
    {
        "claim_id": "CLM-48310",
        "phone": "555-418-4492",
        "claim_type": "Auto glass",
        "status": "Approved",
        "status_detail": "Approved in full on October 10th.",
        "amount": "$4,250.00",
        "adjuster_name": "Rosa Lindqvist",
        "required_documents": "",
        "submission_deadline": "",
        "last_updated": "2026-10-12",
        "next_step": "Payment was issued October 12th and lands in 3 to 5 business days.",
    },
    {
        "claim_id": "CLM-48402",
        "phone": "555-693-6678",
        "claim_type": "Health - outpatient",
        "status": "Denied",
        "status_detail": "Denied because the provider was out of network.",
        "amount": "",
        "adjuster_name": "Jon Mbeki",
        "required_documents": "",
        "submission_deadline": "2026-11-11",
        "last_updated": "2026-10-09",
        "next_step": "You can appeal within 30 days of the decision letter.",
    },
    {
        "claim_id": "CLM-48118",
        "phone": "555-234-1187",
        "claim_type": "Auto rental reimbursement",
        "status": "Paid",
        "status_detail": "Reimbursed on September 28th.",
        "amount": "$310.00",
        "adjuster_name": "Rosa Lindqvist",
        "required_documents": "",
        "submission_deadline": "",
        "last_updated": "2026-09-28",
        "next_step": "No action needed; this claim is closed.",
    },
]

INTERACTION_HEADERS: list[str] = [
    "timestamp",
    "call_id",
    "caller_name",
    "caller_phone",
    "callback_number",
    "kb_consulted",
    "sentiment",
    "intent",
    "authenticated",
    "escalated",
    "outcome",
    "claim_ids",
    "duration_seconds",
    "summary",
    "recording_url",
    "transcript",
    "source",
]

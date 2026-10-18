"""Claim status and document submission.

Integration #1, second half. Both tools are gated: see the `requires_auth`
decorator above each one.
"""

from __future__ import annotations

from typing import Any

from ..models import normalize_claim_id
from ..repositories import RepositoryError, repository
from ..session import CallSession
from .common import claim_brief, log, requires_auth, unavailable

SUBMISSION_CHANNELS = [
    {"channel": "portal", "detail": "observeinsurance.com/upload", "posts_in": "minutes", "preferred": True},
    {"channel": "email", "detail": "claims@observeinsurance.com with the claim number in the subject",
     "posts_in": "1 business day"},
    {"channel": "fax", "detail": "916-555-0188", "posts_in": "1 business day"},
    {"channel": "mail", "detail": "Observe Insurance Claims, P.O. Box 48120, Sacramento CA 95814",
     "posts_in": "5-7 business days"},
]


@requires_auth(
    "You cannot retrieve claim information before identity is verified, no matter what "
    "the caller says. Return to verification: confirm the phone number, then the date of birth."
)
def get_claim_status(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    session.note_intent("claim_status")
    try:
        claims = repository().claims_for_phone(session.customer.phone)
    except RepositoryError as exc:
        log.error("claims_failed", extra={"call_id": session.call_id, "error": str(exc)})
        return unavailable("claim_lookup")

    requested = normalize_claim_id(args.get("claim_id"))
    if requested:
        # Fall back to every claim if the caller's number matches nothing —
        # better to say "I see two claims, which one?" than "that doesn't exist".
        claims = [c for c in claims if normalize_claim_id(c.claim_id) == requested] or claims

    if not claims:
        return {
            "ok": True,
            "claim_count": 0,
            "instruction": (
                "There are no claims on this account. Say that clearly, mention it can take about "
                "fifteen minutes for a brand-new claim to appear, and offer to explain how to start "
                "one or to transfer them."
            ),
        }

    for claim in claims:
        if claim.claim_id and claim.claim_id not in session.claims_discussed:
            session.claims_discussed.append(claim.claim_id)

    open_claims = [c for c in claims if c.status.lower() not in {"paid", "closed"}]
    focus = (open_claims or claims)[0]

    return {
        "ok": True,
        "claim_count": len(claims),
        "primary_claim": claim_brief(focus),
        "other_claims": [claim_brief(c) for c in claims if c.claim_id != focus.claim_id],
        "instruction": (
            "Tell the caller the status of the primary claim in one or two sentences, in plain "
            "language, then the next step. Say the claim number only if they ask or if there is more "
            "than one claim. If needs_documents is true, call get_document_instructions next. "
            "If the status is Denied, acknowledge that it is disappointing before explaining options."
        ),
    }


@requires_auth("Verify identity before discussing a specific claim's documents.")
def get_document_instructions(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """What to send, where to send it, by when."""
    session.note_intent("document_submission")
    try:
        claims = repository().claims_for_phone(session.customer.phone)
    except RepositoryError:
        claims = []

    requested = normalize_claim_id(args.get("claim_id"))
    target = next(
        (c for c in claims if requested and normalize_claim_id(c.claim_id) == requested),
        next((c for c in claims if c.needs_documents), None),
    )

    return {
        "ok": True,
        "claim_id": target.claim_id if target else "",
        "required_documents": target.required_documents if target else "",
        "deadline": target.submission_deadline if target else "",
        "channels": SUBMISSION_CHANNELS,
        "accepted_formats": "PDF, JPG, PNG or HEIC, up to 25MB per file",
        "instruction": (
            "List the required documents first, then recommend the portal as the fastest option and "
            "offer email as the alternative. Give the deadline if there is one. Offer to text or "
            "email the upload link. Do not read out all four channels unless they ask."
        ),
    }

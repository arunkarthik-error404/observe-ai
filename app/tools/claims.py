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


def greeting_note(session: CallSession) -> str:
    """A reminder to confirm the caller by name, for the agent that speaks next.

    In squad mode `verify_identity` returns to the triage agent, which then
    transfers — on a live call, 580ms later, without saying anything. The
    specialist that picks up is told never to greet, correctly, because the
    caller does not know a handoff happened. Between the two rules the
    confirmation had no owner: the caller went from reciting a date of birth
    straight to claim data, never hearing their name or that the check passed.

    So the duty is re-issued to whoever actually speaks, and only when the
    handoff really did intervene — in single-assistant mode the agent that was
    told is the agent that speaks, and this stays silent. The model is asked to
    check the transcript first, because triage sometimes does get its sentence
    out, and hearing it twice is its own kind of wrong.
    """
    if not (session.greeting_owed and session.customer):
        return ""
    if session.verified_by and session.verified_by == session.current_assistant:
        return ""
    session.greeting_owed = False
    return (
        f"The caller was verified a moment ago and then handed to you, so they may never have "
        f"been told. Look back over the conversation: if nobody has confirmed it since they gave "
        f"their date of birth, open with one short sentence naming them — \"Thanks, "
        f"{session.customer.greeting_name} — you're all verified\" — before anything below. If it "
        f"was already said, do not say it again. "
    )


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

    greeting = greeting_note(session)

    if not claims:
        return {
            "ok": True,
            "claim_count": 0,
            "instruction": greeting + (
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
        "instruction": greeting + (
            "Tell the caller the status of the primary claim in one or two sentences, in plain "
            "language, then the next step. "
            # The old wording stopped here, and on a live call the agent recited
            # both of a caller's claims in full — every field of each. The
            # caller rang about one of them; reading out the closed one too is
            # a wall of speech they cannot skim.
            "If claim_count is more than one, say how many there are first, give the primary "
            "claim's status, then mention what the other one is in a single clause and let them "
            "ask. Do not recite the details of more than one claim unless they ask. "
            "Say a claim number only if they ask or if there is more than one claim. "
            "When you say a money amount out loud, read amount_spoken, never amount. "
            "If needs_documents is true, call get_document_instructions next. "
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

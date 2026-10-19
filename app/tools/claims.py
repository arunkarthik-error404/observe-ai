"""Claim status, including what to send when documents are outstanding.

Integration #1, second half. Gated: see the `requires_auth` decorator.

This was two tools until the document half was folded in. `get_claim_status`
ended its own instruction with "if needs_documents is true, call
get_document_instructions next" — a second round trip, guaranteed, for data
this tool already held when it answered the first one. It cost a measured
~1.9s of the one scenario where the caller is already waiting to be told what
to do, and the model could skip it. Now the documents ride back with the
status that says they are needed, and the tool count drops with them.
"""

from __future__ import annotations

from typing import Any

from ..models import normalize_claim_id, speakable
from ..repositories import RepositoryError, repository
from ..session import CallSession
from .common import claim_brief, log, requires_auth, unavailable

# Written forms, kept readable here. They are rewritten for the voice on the
# way out — the knowledge base had the same strings and the same problem, and
# fixing only that half left this tool still able to say an email address with
# no "at" in it.
SUBMISSION_CHANNELS = [
    {"channel": "portal", "detail": "observeinsurance.com/upload", "posts_in": "minutes", "preferred": True},
    {"channel": "email", "detail": "claims@observeinsurance.com with the claim number in the subject",
     "posts_in": "1 business day"},
    {"channel": "fax", "detail": "916-555-0188", "posts_in": "1 business day"},
    {"channel": "mail", "detail": "Observe Insurance Claims, P.O. Box 48120, Sacramento CA 95814",
     "posts_in": "5-7 business days"},
]


def greeting_note(session: CallSession) -> str:
    """The sentence that tells the caller the security check passed.

    It lives here, on the first tool after verification, rather than on
    `verify_identity` itself — because in squad mode the agent that verifies
    hands the call on about a second later, and twice on live calls the
    transfer severed the sentence mid-word ("You're all"). The agent reading
    this one is always an agent that is staying.

    One owner, unconditionally: when both `verify_identity` and this asked for
    it, the caller heard their name twice. It fires once per call and then goes
    quiet, and in single-assistant mode it is simply the same agent a moment
    later, so nothing about that flow changes.

    No quotation marks around the example. The previous version wrapped it in
    them and the model read the closing quote out: "You're all verified.'"
    """
    if not (session.greeting_owed and session.customer):
        return ""
    session.greeting_owed = False
    return (
        f"First, before anything below, tell the caller the check passed — one short sentence "
        f"naming them, such as: Thanks, {session.customer.greeting_name}, you're all verified. "
        f"Nobody has told them yet; it is the only signal they get. Say it plainly, with no "
        f"quotation marks and nothing added, then continue. "
    )


def _document_block(claim: Any) -> dict[str, Any]:
    """What to send, where, and by when — for a claim that is waiting on it."""
    return {
        "claim_id": claim.claim_id,
        "required_documents": claim.required_documents,
        "deadline": speakable(claim.submission_deadline),
        "channels": [{**c, "detail": speakable(c["detail"])} for c in SUBMISSION_CHANNELS],
        "accepted_formats": "PDF, JPG, PNG or HEIC, up to 25 megabytes per file",
    }


DOCUMENT_INSTRUCTION = (
    "This claim is waiting on documents, and `documents` below has them. Name the required "
    "documents, then recommend the portal as the fastest option and offer email as the "
    "alternative. Give the deadline. "
    # Both halves of this used to live on the separate tool, and a caller who
    # went straight to documents heard the deadline as "the 20 ninth of
    # October" and then "29 October" — the words rewritten as digits, which is
    # the bug the spoken form exists to prevent.
    "The deadline and the portal address are already written the way they are said. Read them "
    "across exactly: say the twenty-ninth of October, never 29 October or October 29th; say the "
    "address with its forward slash. Do not turn any of it back into figures or symbols. "
    # This used to end "Offer to text or email the upload link", and nothing in
    # this system can send anything. On a live call the agent offered it
    # unprompted, the caller said yes, and it had to answer "I can't send the
    # upload link directly" — an offer made and withdrawn in two turns.
    "You cannot send, text or email anything, so never offer to. Read the portal address aloud "
    "and tell them it is also on their statement. "
    "Do not read out all four channels unless they ask."
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

    # The claim that is actually waiting on paperwork — the one in focus if it
    # is, otherwise any other. Picked rather than assumed: the old separate
    # tool searched for it too, and a caller whose open claim is fine while a
    # second one needs documents should still be told.
    pending = focus if focus.needs_documents else next(
        (c for c in claims if c.needs_documents), None)

    payload: dict[str, Any] = {
        "ok": True,
        "claim_count": len(claims),
        "primary_claim": claim_brief(focus),
        "other_claims": [claim_brief(c) for c in claims if c.claim_id != focus.claim_id],
    }
    if pending is not None:
        payload["documents"] = _document_block(pending)

    payload["instruction"] = greeting + (
        "Tell the caller the status of the primary claim in one or two sentences, in plain "
        "language, then the next step. "
        # The old wording stopped here, and on a live call the agent recited
        # both of a caller's claims in full — every field of each. The caller
        # rang about one of them; reading out the closed one too is a wall of
        # speech they cannot skim.
        "If claim_count is more than one, say how many there are first, give the primary "
        "claim's status, then mention what the other one is in a single clause and let them "
        "ask. Do not recite the details of more than one claim unless they ask. "
        "Say a claim number only if they ask or if there is more than one claim. "
        # Removing the figure form from the payload was not enough: the model
        # rewrote the words back into digits. Proven rather than assumed — two
        # calls with identical payload text produced "the twelfth of October"
        # on one and "12 October" on the other, so the variation is the
        # model's, not the transcript's.
        "amount_spoken and every date here are already words, and words are what you say. "
        "Do not convert them back into figures: say \"four thousand two hundred and fifty "
        "dollars\", never \"4250 dollars\"; say \"the twelfth of October\", never \"12 "
        "October\" or \"October 12th\". Read them across exactly as they are written here. "
        "If the status is Denied, acknowledge that it is disappointing before explaining options. "
        # A live call began "your claim for health outpatient services has been
        # denied because the provider was out of network" 1.7s before this tool
        # answered — the status, the reason and the claim type, all narrated
        # ahead of the data. It was right. The one before guessed a claim count
        # and was also right. Nothing stops the sentence, so the result has to
        # be able to take it back.
        "And if anything you have already said about this claim differs from what is here, "
        "correct it plainly before going on. What is below is the record; what you said before "
        "reading it was a guess."
    ) + (" " + DOCUMENT_INSTRUCTION if pending is not None else "")
    return payload

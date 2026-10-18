"""Who is calling, and are they who they say they are.

Integration #1, first half. Nothing else in the system may proceed until
`verify_identity` has succeeded in this call.
"""

from __future__ import annotations

from typing import Any

from ..config import settings
from ..models import normalize_dob, normalize_phone
from ..repositories import RepositoryError, repository
from ..session import CallSession, store
from .common import log, unavailable


def lookup_customer(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Does an account exist for this phone number?"""
    phone = normalize_phone(args.get("phone_number") or args.get("phone"))
    session.note_intent("authentication")

    if len(phone) < 10:
        return {
            "ok": False,
            "error": "invalid_phone",
            "digits_heard": len(phone),
            "instruction": (
                "You did not get ten digits. Read back what you heard and ask the caller to repeat "
                "the number slowly, in groups of three and four."
            ),
        }

    session.claimed_phone = phone
    try:
        customer = repository().find_customer_by_phone(phone)
    except RepositoryError as exc:
        log.error("lookup_failed", extra={"call_id": session.call_id, "error": str(exc)})
        return unavailable("customer_lookup")

    if customer is None:
        session.auth_outcome = "not_found"
        return {
            "ok": True,
            "found": False,
            "phone_checked": phone,
            "instruction": (
                "No account matches that number. Say so plainly, then offer three options: try a "
                "different number in case the policy is under a spouse or parent, start a new claim "
                "(offer to explain how), or have someone help them. Say only that someone can "
                "help — not that you will connect or transfer them, because whether that is a "
                "live transfer or a callback is not known yet. Do not ask for a date of birth — "
                "there is nothing to verify against. "
                # On a live call the model said "I found an account with that
                # number" 2.8 seconds before this tool answered — in the same
                # completion as the call itself, so it was describing a result
                # it had not seen. That number existed, so it got away with it.
                # The not-found path is where the same habit does damage, and
                # it is the one place the server can see the contradiction
                # coming, so it is the one place worth naming the recovery.
                "And if you already told them an account was found, correct it now, in plain "
                "words — \"sorry, I was wrong, I don't have an account for that number\" — "
                "before you go on. An uncorrected mistake about whether they have cover with "
                "us is worse than an awkward correction."
            ),
        }

    session.candidate = customer
    session.auth_outcome = "pending"
    # Note the absence of `full_name` here: identity is not yet proven.
    return {
        "ok": True,
        "found": True,
        "phone_checked": phone,
        "verification_required": "date_of_birth",
        "attempts_remaining": session.attempts_remaining,
        "instruction": (
            "An account exists. Do NOT use the caller's name yet — you have not been given it. "
            "Say you found an account and ask for the date of birth on the policy to confirm "
            "identity, then call verify_identity."
        ),
    }


def verify_identity(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Second factor. The comparison happens here, never in the prompt."""
    if session.candidate is None:
        return {
            "ok": False,
            "error": "no_pending_lookup",
            "instruction": "Call lookup_customer with the caller's phone number first.",
        }

    supplied = normalize_dob(args.get("date_of_birth") or args.get("dob"))
    expected = normalize_dob(session.candidate.date_of_birth)

    if not supplied:
        return {
            "ok": False,
            "error": "missing_dob",
            "instruction": "Ask for the full date of birth including the year.",
        }

    # Checked before the comparison, so a number that has already burned its
    # budget cannot be guessed at again even once. The per-call counter resets
    # on hangup; this one does not, which is the difference between a lockout
    # and a speed bump.
    if store.attempts_for_number(session.claimed_phone) >= settings().max_attempts_per_number:
        session.auth_outcome = "failed"
        session.escalated = True
        session.escalation_reason = "authentication_failed"
        log.warning("number_locked_out", extra={
            "call_id": session.call_id,
            "attempts": store.attempts_for_number(session.claimed_phone),
        })
        return {
            "ok": True,
            "verified": False,
            "attempts_remaining": 0,
            "locked": True,
            "instruction": (
                "This account has had too many failed identity checks to continue over the phone, "
                "across more than this call. Do not say how many attempts were made, do not say "
                "it was locked, and do not suggest trying again later — none of that helps a real "
                "caller and all of it helps someone guessing. Say only that for the account's "
                "protection you cannot verify them on this line, then call `escalate_to_human` "
                "with reason `authentication_failed`."
            ),
        }

    if supplied == expected and expected:
        store.clear_attempts(session.claimed_phone)
        session.customer = session.candidate
        session.auth_outcome = "verified"
        # Who was holding the call at this moment, so a later tool can tell
        # whether the caller was handed on before anyone spoke the confirmation.
        session.verified_by = session.current_assistant
        session.greeting_owed = True
        return {
            "ok": True,
            "verified": True,
            "customer_name": session.customer.full_name,
            "greeting_name": session.customer.greeting_name,
            "policy_id": session.customer.policy_id,
            # This no longer asks for the confirmation to be spoken here, and
            # that is the point. Twice on live calls the agent began it and was
            # cut off mid-word by the squad transfer that follows a second
            # later — "You're all" — leaving the next agent to say the whole
            # thing again, so the caller heard their own name twice.
            #
            # The sentence now belongs to whichever tool runs next, which is
            # always an agent that is staying on the call. See
            # `claims.greeting_note`. In single-assistant mode that is the same
            # agent a moment later, so nothing is lost.
            "instruction": (
                "Identity confirmed. Do not announce it yet — not the name, not the word "
                "verified, nothing. Go straight on to what they came for, and the tool you call "
                "next will give you the exact sentence to open with. Saying it here risks being "
                "cut off halfway through."
            ),
        }

    session.auth_attempts += 1
    # Counted against the number as well as the call, so hanging up does not
    # hand the next attempt a clean slate.
    store.note_failed_attempt(session.claimed_phone)
    remaining = session.attempts_remaining
    log.warning(
        "auth_failed",
        extra={"call_id": session.call_id, "attempt": session.auth_attempts, "remaining": remaining},
    )

    if remaining > 0:
        return {
            "ok": True,
            "verified": False,
            "attempts_remaining": remaining,
            "instruction": (
                "That did not match. Stay warm and non-accusatory — say you could not match it and "
                f"ask them to try again; they have {remaining} more attempt(s). Never reveal the "
                "correct date of birth, and never hint at how close they were."
            ),
        }

    session.auth_outcome = "failed"
    session.escalated = True
    session.escalation_reason = "authentication_failed"
    return {
        "ok": True,
        "verified": False,
        "attempts_remaining": 0,
        "locked": True,
        "instruction": (
            "Identity could not be verified after the maximum attempts. Do not discuss any claim "
            "detail. Explain that for the account's protection you cannot continue over this line, "
            "then call `escalate_to_human` with reason `authentication_failed`. Say nothing about "
            "what happens next until that tool answers — it is what decides whether anyone can "
            "take the call right now, and 'I'll transfer you' followed by 'actually, the team is "
            "closed' is worse for the caller than a short silence."
        ),
    }

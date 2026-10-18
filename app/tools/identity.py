"""Who is calling, and are they who they say they are.

Integration #1, first half. Nothing else in the system may proceed until
`verify_identity` has succeeded in this call.
"""

from __future__ import annotations

from typing import Any

from ..models import normalize_dob, normalize_phone
from ..repositories import RepositoryError, repository
from ..session import CallSession
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
                "(offer to explain how), or be transferred to a representative. Do not ask for a "
                "date of birth — there is nothing to verify against."
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

    if supplied == expected and expected:
        session.customer = session.candidate
        session.auth_outcome = "verified"
        return {
            "ok": True,
            "verified": True,
            "customer_name": session.customer.full_name,
            "greeting_name": session.customer.greeting_name,
            "policy_id": session.customer.policy_id,
            "instruction": (
                "Identity confirmed. Thank the caller by first name, then ask what they need help "
                "with, or go straight to their claim if they already said."
            ),
        }

    session.auth_attempts += 1
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
            "then hand the caller to a representative."
        ),
    }

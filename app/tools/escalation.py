"""Getting the caller to a human, and getting out of the way in a crisis."""

from __future__ import annotations

import hashlib
from typing import Any

from ..config import settings
from ..models import normalize_phone, utc_now_iso
from ..notifications import notify_callback_number, notify_escalation
from ..repositories import repository
from ..session import CallSession
from .common import EMERGENCY_SCRIPT, business_hours_now, log

MIN_PHONE_DIGITS = 10

# Reason codes that assert something about the identity check. Everything else
# ("caller_request", "distressed_caller", …) describes the caller's own
# behaviour, which only the model was there for, so it is taken at its word.
IDENTITY_REASONS = {"customer_not_found": "not_found", "authentication_failed": "failed"}


def _record_url(session: CallSession) -> str:
    """Never let a broken link break an escalation."""
    if not session.customer:
        return ""
    try:
        return repository().record_url(session.customer)
    except Exception:  # noqa: BLE001 - a dead CRM must not block the handoff
        return ""


def _checked_reason(session: CallSession, claimed: str) -> tuple[str, str]:
    """Hold an identity-related reason code against what actually happened.

    Same principle as the auth gate: the model narrates, the server decides.
    A live call escalated as `customer_not_found` for a caller who could not
    remember their number — `lookup_customer` never ran, so nothing was ever
    looked up, let alone not found. The ticket read as "we searched and they do
    not exist", which is a different problem with a different fix, and it skews
    every count built on the reason field.

    Returns the reason to use and, when it differs, the one the model claimed —
    kept in the handoff so the correction is visible rather than silent.
    """
    expected = IDENTITY_REASONS.get(claimed)
    if expected is None or session.auth_outcome == expected:
        return claimed, ""
    # The claim does not hold. Which is true depends on how far identity got.
    corrected = "no_identifier_given" if not session.claimed_phone else "verification_incomplete"
    return corrected, claimed


def _number_to_ring(session: CallSession) -> str:
    """The number a human should dial, preferring one given for the purpose."""
    return session.callback_number or session.claimed_phone


def _callback_number_instruction(number: str) -> str:
    """What to say about the number, which depends on whether we have one.

    The old instruction said "offer a callback on this number" unconditionally.
    On a web call there is no caller ID, so "this number" referred to nothing —
    the agent asked for a number, the caller read one out, and it reached no
    field a human could act on. Promising a callback with nowhere to ring is
    the one failure this whole path exists to prevent.
    """
    if number:
        return (
            " Read the number we have for them back one digit at a time and ask them to confirm "
            "it is the right one. If they give a different number, call `record_callback_number` "
            "with it."
        )
    return (
        " You do not have a number to reach them on, and a callback cannot happen without one. "
        "Ask for the best number, call `record_callback_number` with it, then read it back one "
        "digit at a time to confirm. Do not end the call before you have done that."
    )


def escalate_to_human(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Opens a handoff ticket, then says how the handoff will happen."""
    reason, reason_reported = _checked_reason(
        session, (args.get("reason") or "caller_request").strip()
    )
    if reason_reported:
        log.info("escalation_reason_corrected", extra={
            "call_id": session.call_id, "reported": reason_reported, "reason": reason,
            "auth_outcome": session.auth_outcome,
        })
    urgency = (args.get("urgency") or "normal").strip()
    notes = (args.get("notes") or "").strip()

    # Accepted here as well as on `record_callback_number` because sometimes the
    # caller volunteers the number before anyone asks, and making them repeat it
    # is exactly the "start over" experience the handoff context exists to avoid.
    supplied = normalize_phone(args.get("callback_number") or args.get("phone_number"))
    if len(supplied) >= MIN_PHONE_DIGITS:
        session.callback_number = supplied

    session.escalated = True
    session.escalation_reason = reason
    session.note_intent("escalation")

    # Four digits, because this gets read aloud. The first version was
    # ESC-17761-296, which on a live call came back as "ESC minus 1 60 one-two
    # 96" followed by TTS gibberish — a reference number nobody could write
    # down. The call id is the real key and it is in the handoff context; this
    # is only what a human says to another human.
    #
    # sha256 rather than hash(): Python salts string hashing per process, so
    # the same call produced ESC-4982, ESC-3744 and ESC-6060 on three runs —
    # an id that changes if the server restarts between a caller writing it
    # down and a human looking it up. Four digits still collide (about a 46%
    # chance somewhere in 100 tickets), which is why nothing is ever looked up
    # by this: Call_Id__c is the key in Salesforce and the handoff carries it.
    digest = hashlib.sha256(session.call_id.encode()).hexdigest()
    ticket_id = f"ESC-{int(digest[:8], 16) % 9000 + 1000}"
    session.ticket_id = ticket_id
    in_hours = business_hours_now()

    # The warm-handoff context a human agent needs so the caller does not repeat themselves.
    handoff_context = {
        "ticket_id": ticket_id,
        "created_at": utc_now_iso(),
        "reason": reason,
        # Only present when the server disagreed with the model, so a human can
        # see that the code was adjusted and why the notes may read oddly.
        "reason_reported": reason_reported,
        "urgency": urgency,
        "notes": notes,
        "caller_phone": session.claimed_phone,
        # The number to ring, not only one volunteered for the purpose. This
        # read the raw field, so a live ticket went to Slack with
        # callback_number "" while the tool result beside it carried the
        # caller's number — the message was still right, because
        # `_number_to_dial` falls back to caller_phone, but anyone reading the
        # logged handoff saw a blank where a number was.
        "callback_number": _number_to_ring(session),
        "identity_verified": session.authenticated,
        "customer_name": session.customer.full_name if session.customer else "unverified",
        "policy_id": session.customer.policy_id if session.customer else "",
        "claims_discussed": session.claims_discussed,
        "auth_attempts": session.auth_attempts,
        "topics": session.intents,
        # One click to the caller's record, rather than the receiving agent
        # searching for them while the caller waits. Empty for a backend with
        # no addressable records, and the message simply omits the button.
        "record_url": _record_url(session),
    }
    log.info("escalation_ticket", extra={"call_id": session.call_id, "ticket": handoff_context})

    # Three outcomes, not two. Promising to "connect you now" when no transfer
    # destination is configured leaves the caller holding a line that goes
    # nowhere — which is exactly what happened on the first live squad call.
    if in_hours and settings().transfer_phone_number:
        notify_escalation(handoff_context, "live")
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "transfer": "live",
            "queue": "claims_support",
            "estimated_wait": "about two minutes",
            "handoff_context": handoff_context,
            "instruction": (
                "Tell the caller you are connecting them to a representative now, that the wait is "
                "about two minutes, and that you have passed along the details so they will not have "
                "to start over. Give the reference number only if they ask. Then transfer the call."
            ),
        }

    if in_hours:
        handoff_context["callback_window"] = "within the next few minutes"
        notify_escalation(handoff_context, "callback")
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "transfer": "callback",
            "callback_window": "within the next few minutes",
            "callback_number": _number_to_ring(session),
            "handoff_context": handoff_context,
            "instruction": (
                "Every representative is busy. Tell the caller a representative will call them back "
                "within the next few minutes and that you have passed along the details so they "
                "will not have to start over."
                + _callback_number_instruction(_number_to_ring(session))
                + " Offer the four-digit reference number only if they ask for it, and read it one "
                "digit at a time. Then ask if there is anything else and end the call. Do not say "
                "you are connecting them now."
            ),
        }

    handoff_context["callback_window"] = "first thing on the next working day"
    notify_escalation(handoff_context, "callback")
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "transfer": "callback",
        "callback_window": "first thing on the next working day",
        "callback_number": _number_to_ring(session),
        "office_hours": settings().office_hours,
        "handoff_context": handoff_context,
        "instruction": (
            "The team is closed right now. Apologise, give the office hours, and offer a callback "
            "as the first thing on the next working day."
            + _callback_number_instruction(_number_to_ring(session))
            + " Reassure them it is logged. Offer the four-digit reference number only if they ask "
            "for it, and read it one digit at a time. Then end the call."
        ),
    }


def record_callback_number(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Store the number a human should ring, and put it on the open ticket.

    Separate from `escalate_to_human` because of the order a real call happens
    in: the agent only learns a callback is needed from that tool's *result*,
    so the number arrives a turn or two later. Folding it into the escalation
    argument alone would mean the one case that matters — the caller who cannot
    remember the number on their policy — is the case it never captures.

    Validation is the same shape as `lookup_customer`: an STT layer that drops
    a digit should produce a read-back, not a callback to a wrong number.
    """
    number = normalize_phone(args.get("callback_number") or args.get("phone_number"))
    if len(number) < MIN_PHONE_DIGITS:
        return {
            "ok": False,
            "error": "invalid_phone",
            "digits_heard": len(number),
            "instruction": (
                "You did not get ten digits. Read back what you heard and ask the caller to repeat "
                "the number slowly, in groups of three and four, then call this tool again."
            ),
        }

    # What a human would already be looking at, captured before the write.
    already_on_ticket = _number_to_ring(session)
    session.callback_number = number
    log.info("callback_number_recorded",
             extra={"call_id": session.call_id, "ticket": session.ticket_id,
                    "callback_number": number, "changed": number != already_on_ticket})

    # Only worth a Slack follow-up once a ticket is open — otherwise there is
    # no handoff for a human to attach it to, and the number still reaches the
    # post-call record either way.
    #
    # And only when the number actually changed. The escalation instruction
    # says to call this tool if the caller gives a *different* number, but on a
    # live call the caller confirmed the one read back to them and the model
    # called it anyway — posting a second Slack message telling a human to ring
    # a number already on the card. A notification that carries no new
    # information trains people to stop reading them.
    notified = False
    if session.ticket_id and number != already_on_ticket:
        notified = notify_callback_number({
            "ticket_id": session.ticket_id,
            "callback_number": number,
            "customer_name": session.customer.full_name if session.customer else "unverified",
            "identity_verified": session.authenticated,
        })

    return {
        "ok": True,
        "callback_number": number,
        "ticket_id": session.ticket_id,
        "notified": notified,
        # A read-back the caller has already given is not reassurance, it is
        # the call not ending. Only ask for one when the number is new.
        "instruction": (
            "Saved. Read the number back one digit at a time, confirm it is right, and tell them a "
            "representative will call them on it. If they correct it, call this tool again with the "
            "new number."
            if number != already_on_ticket
            else "That is the number already on the ticket, so nothing changed. Do not read it back "
            "again — the caller has confirmed it. Say it is logged and move on to closing the call."
        ),
    }


def report_emergency(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Explicit safety path. Claim handling stops; nothing else matters here."""
    session.emergency = True
    session.escalated = True
    session.escalation_reason = "emergency"
    session.note_intent("emergency")
    log.error(
        "emergency_declared",
        extra={"call_id": session.call_id, "description": args.get("description", "")},
    )
    return {
        "ok": True,
        "emergency": True,
        "say_verbatim": EMERGENCY_SCRIPT,
        "instruction": (
            "Say the text in say_verbatim immediately, with nothing before it — no 'just a sec', no "
            "filler. Do not continue with claim business, do not ask for a date of birth, do not "
            "offer the portal. Then END THE CALL, after one short line such as 'I'll let you go now "
            "so you can call them.' This overrides the usual rule about not ending a call: the "
            "caller cannot dial emergency services while they are still connected to you, so "
            "staying on the line is the harmful choice. Only stay on if they clearly say they are "
            "safe and want to continue."
        ),
    }

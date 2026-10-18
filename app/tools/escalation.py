"""Getting the caller to a human, and getting out of the way in a crisis."""

from __future__ import annotations

from typing import Any

from ..config import settings
from ..models import utc_now_iso
from ..notifications import notify_escalation
from ..repositories import repository
from ..session import CallSession
from .common import EMERGENCY_SCRIPT, business_hours_now, log


def _record_url(session: CallSession) -> str:
    """Never let a broken link break an escalation."""
    if not session.customer:
        return ""
    try:
        return repository().record_url(session.customer)
    except Exception:  # noqa: BLE001 - a dead CRM must not block the handoff
        return ""


def escalate_to_human(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Opens a handoff ticket, then says how the handoff will happen."""
    reason = (args.get("reason") or "caller_request").strip()
    urgency = (args.get("urgency") or "normal").strip()
    notes = (args.get("notes") or "").strip()

    session.escalated = True
    session.escalation_reason = reason
    session.note_intent("escalation")

    # Four digits, because this gets read aloud. The first version was
    # ESC-17761-296, which on a live call came back as "ESC minus 1 60 one-two
    # 96" followed by TTS gibberish — a reference number nobody could write
    # down. The call id is the real key and it is in the handoff context; this
    # is only what a human says to another human.
    ticket_id = f"ESC-{abs(hash(session.call_id)) % 9000 + 1000}"
    in_hours = business_hours_now()

    # The warm-handoff context a human agent needs so the caller does not repeat themselves.
    handoff_context = {
        "ticket_id": ticket_id,
        "created_at": utc_now_iso(),
        "reason": reason,
        "urgency": urgency,
        "notes": notes,
        "caller_phone": session.claimed_phone,
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
            "handoff_context": handoff_context,
            "instruction": (
                "Every representative is busy. Tell the caller a representative will call them back "
                "on this number within the next few minutes and that you have passed along the "
                "details so they will not have to start over. Offer the four-digit reference number "
                "only if they ask for it, and read it one digit at a time. Then ask if there is "
                "anything else and end the call. Do not say you are connecting them now."
            ),
        }

    handoff_context["callback_window"] = "first thing on the next working day"
    notify_escalation(handoff_context, "callback")
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "transfer": "callback",
        "callback_window": "first thing on the next working day",
        "office_hours": settings().office_hours,
        "handoff_context": handoff_context,
        "instruction": (
            "The team is closed right now. Apologise, give the office hours, and offer a callback on "
            "this number as the first thing on the next working day. Confirm the number back to them "
            "and reassure them it is logged. Offer the four-digit reference number only if they ask "
            "for it, and read it one digit at a time. Then end the call."
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

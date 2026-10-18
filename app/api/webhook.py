"""The Vapi webhook: tool calls in, post-call records out.

Vapi posts every server event to one URL with a `message.type` discriminator,
so this is mostly a router over that discriminator.

**A tool call must always return 200.** If the server 500s, Vapi hands the
model nothing and the caller hears dead air. Errors are therefore converted
into tool *results* that tell the model what to say. The one deliberate
exception is the post-call write: a non-2xx there is how Vapi's redelivery is
triggered, and no caller is waiting on it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from ..models import InteractionRecord, utc_now_iso
from ..repositories import RepositoryError, repository
from ..session import store
from ..tools import dispatch
from . import payloads
from .security import auth_header_names, require_secret, token_matches

log = logging.getLogger("claims.api")
router = APIRouter(tags=["vapi"])

PASSIVE_EVENTS = {
    "status-update", "speech-update", "transcript", "hang", "conversation-update",
    "user-interrupted", "voice-input", "model-output",
}


@router.post("/vapi/webhook")
async def vapi_webhook(request: Request, x_vapi_secret: str | None = Header(default=None)) -> Any:
    """Header-authenticated entry point. Per-tool server config uses this."""
    require_secret(x_vapi_secret, request)
    return await handle_message(request)


@router.post("/vapi/webhook/{token}")
async def vapi_webhook_tokened(
    token: str, request: Request, x_vapi_secret: str | None = Header(default=None)
) -> Any:
    """URL-authenticated entry point for assistant-level server messages."""
    if not token_matches(token):
        require_secret(x_vapi_secret, request)
    return await handle_message(request)


async def handle_message(request: Request) -> Any:
    body = await request.json()
    message = body.get("message") or body
    msg_type = message.get("type", "unknown")
    call_id = payloads.call_id(message)
    log.info("webhook_received", extra={
        "call_id": call_id,
        "message_type": msg_type,
        "auth_headers_seen": auth_header_names(request),
    })

    if msg_type in {"tool-calls", "function-call"}:
        return _handle_tool_calls(call_id, message, msg_type)

    if msg_type == "end-of-call-report":
        return _handle_end_of_call(call_id, message)

    if msg_type in PASSIVE_EVENTS:
        if msg_type == "status-update":
            log.info("call_status", extra={"call_id": call_id, "status": message.get("status")})
            if message.get("status") == "ended":
                log.info("call_ended", extra={"call_id": call_id, "reason": message.get("endedReason")})
        return {"ok": True}

    log.info("unhandled_message", extra={"call_id": call_id, "message_type": msg_type})
    return {"ok": True}


def _handle_tool_calls(call_id: str, message: dict[str, Any], msg_type: str) -> Any:
    session = store.get(call_id)
    if not session.claimed_phone:
        # Inbound PSTN calls carry caller ID; seed it so the agent can offer
        # "is it the number you're calling from?" instead of making them recite it.
        session.claimed_phone = payloads.caller_number(message) or ""

    # In squad mode this records the handoff chain. Falls back to the assistant
    # id because Vapi does not always include the full assistant object.
    label = payloads.assistant_label(message)
    if label:
        session.current_assistant = label
        if label not in session.assistant_path:
            session.assistant_path.append(label)

    results = []
    for call in payloads.tool_calls(message):
        outcome = dispatch(session, call["name"], call["arguments"])
        results.append({"toolCallId": call["id"], "name": call["name"], "result": outcome})

    if not results:
        log.warning("tool_call_without_payload", extra={"call_id": call_id})
        return {"results": []}
    if msg_type == "function-call":  # legacy single-function shape
        return {"result": results[0]["result"]}
    return {"results": results}


def _concluded_outcome(session: Any) -> str:
    """The auth state, but only once it has actually concluded.

    `pending` is what a session starts as, not something a call ended as; on a
    completed record it reads as work still to do.

    This is the last fallback rather than a safety net. Carrying the outcome
    forward on the session is what actually stops the downgrade, because the
    Salesforce `Subject` is derived from this value and is sent on every write
    — unlike the custom fields, an empty outcome does not leave the stored one
    alone. A session lost to its TTL or a restart before the end-of-call report
    arrives therefore still writes a generic subject over a specific one; the
    window is one call long, and closing it properly means moving the outcome
    off `Subject` and into a field of its own.
    """
    if session is None or session.auth_outcome == "pending":
        return ""
    return str(session.auth_outcome)


def _handle_end_of_call(call_id: str, message: dict[str, Any]) -> Any:
    """Integration #2, primary path: write the post-call record."""
    analysis = message.get("analysis") or {}
    structured = analysis.get("structuredData") or {}
    session = store.peek(call_id)

    summary = (analysis.get("summary") or message.get("summary") or "").strip()
    if not summary and not (session and session.tool_events):
        # A call that never carried audio (mic blocked, instant hangup) produces
        # an empty report. Writing a blank row for it just adds noise a human
        # has to scroll past; the call still exists in the provider's own logs.
        log.info("interaction_skipped", extra={"call_id": call_id, "reason": "no_activity"})
        return {"ok": True, "logged": False, "reason": "no_activity"}

    record = InteractionRecord(
        call_id=call_id,
        timestamp=message.get("endedAt") or utc_now_iso(),
        caller_name=(
            (session.customer.full_name if session and session.customer else None)
            or structured.get("caller_name")
            or structured.get("callerName")
            or "Unverified caller"
        ),
        caller_phone=(session.claimed_phone if session else "") or payloads.caller_number(message),
        callback_number=(session.callback_number if session else ""),
        summary=summary,
        sentiment=payloads.sentiment_from(analysis, structured),
        intent=", ".join(session.intents) if session and session.intents
        else payloads.intent_from(analysis, structured),
        authenticated=bool(session.authenticated) if session else False,
        escalated=bool(session.escalated) if session else bool(structured.get("escalated")),
        # In squad mode Vapi ignored the per-assistant structured-data schema and
        # returned its own default fields, so this reads both shapes and falls
        # back to session state. Never depend on a provider's extraction alone.
        #
        # The session's own `outcome` sits ahead of `auth_outcome` because this
        # write lands on top of the in-call one. A squad call whose
        # structuredData carried no outcome field used to fall straight through
        # to `auth_outcome` and relabel a record the agent had already logged as
        # `callback_scheduled` back to `pending` — the second, poorer write
        # winning. See `_concluded_outcome`.
        outcome=str(
            structured.get("outcome")
            or structured.get("call_disposition")
            or (session.outcome if session else "")
            or _concluded_outcome(session)
        )[:200],
        claim_ids=", ".join(session.claims_discussed) if session else str(structured.get("claim_id", "")),
        duration_seconds=payloads.duration_from(message),
        recording_url=payloads.recording_url(message),
        transcript=payloads.transcript_from(message),
    )

    # What the caller actually experienced, turn by turn. Logged separately
    # from the record because it is operational data, not claims data — and
    # because a latency regression shows up here long before anyone complains
    # that "the agent feels slow".
    latency = payloads.latency_summary(message)
    if latency:
        if session is not None:
            session.latency = latency
        tool_ms = [round(e.duration_ms) for e in session.tool_events] if session else []
        log.info("call_latency", extra={
            "call_id": call_id,
            **latency,
            "our_tool_ms_total": sum(tool_ms),
            "our_tool_ms_max": max(tool_ms) if tool_ms else 0,
        })

    try:
        repository().write_interaction(record)
        log.info("interaction_logged", extra={"call_id": call_id, "sentiment": record.sentiment})
    except RepositoryError as exc:
        # Vapi retries on a non-2xx, so returning 500 here *is* the retry
        # mechanism. The record is also mirrored to the log as a dead letter.
        log.error("interaction_write_failed", extra={"call_id": call_id, "error": str(exc),
                                                     "dead_letter": record.model_dump()})
        return JSONResponse(status_code=500, content={"ok": False, "error": "write_failed"})

    return {"ok": True, "logged": True}

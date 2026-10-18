"""Reading Vapi's webhook payloads.

Kept apart from the route handlers because this is where a provider's shape
leaks into the system: message envelopes differ between versions, tool calls
arrive in two shapes, and arguments sometimes come as a JSON string. Isolating
it means a change at Vapi's end touches one file.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import normalize_phone


def call_id(message: dict[str, Any]) -> str:
    call = message.get("call") or {}
    return str(call.get("id") or message.get("callId") or "unknown-call")


def caller_number(message: dict[str, Any]) -> str:
    call = message.get("call") or {}
    customer = call.get("customer") or {}
    return normalize_phone(customer.get("number", ""))


def assistant_label(message: dict[str, Any]) -> str:
    """Which squad member is speaking, by name if available, else by id."""
    assistant = message.get("assistant") or {}
    call = message.get("call") or {}
    return assistant.get("name") or assistant.get("id") or call.get("assistantId") or ""


def tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise across Vapi's tool-call shapes (list form and legacy single)."""
    raw = message.get("toolCallList") or message.get("toolCalls") or []
    calls: list[dict[str, Any]] = []
    for item in raw:
        fn = item.get("function") or {}
        args = item.get("arguments", fn.get("arguments"))
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {"raw": args}
        calls.append({
            "id": item.get("id") or item.get("toolCallId") or "",
            "name": item.get("name") or fn.get("name") or "",
            "arguments": args or {},
        })
    if not calls and message.get("functionCall"):
        fc = message["functionCall"]
        calls.append({
            "id": fc.get("id", ""),
            "name": fc.get("name", ""),
            "arguments": fc.get("parameters") or fc.get("arguments") or {},
        })
    return calls


def sentiment_from(analysis: dict[str, Any], structured: dict[str, Any]) -> str:
    """Read both my schema and the default one Vapi substitutes in squad mode."""
    for key in ("sentiment", "caller_sentiment", "callerSentiment"):
        value = structured.get(key)
        if value:
            return str(value).lower()
    evaluation = str(analysis.get("successEvaluation", "")).lower()
    if evaluation in {"true", "pass", "success"}:
        return "positive"
    if evaluation in {"false", "fail"}:
        return "negative"
    return "neutral"


def recording_url(message: dict[str, Any]) -> str:
    """Vapi moved this around; check every place it has lived."""
    artifact = message.get("artifact") or {}
    recording = artifact.get("recording") or {}
    return (
        message.get("recordingUrl")
        or artifact.get("recordingUrl")
        or recording.get("monoUrl")
        or recording.get("stereoUrl")
        or artifact.get("stereoRecordingUrl")
        or ""
    )


def transcript_from(message: dict[str, Any]) -> str:
    """The plain-text transcript, from wherever this payload carries it."""
    artifact = message.get("artifact") or {}
    text = message.get("transcript") or artifact.get("transcript") or ""
    if text:
        return str(text)

    # Fall back to rebuilding it from the message list, which is present even
    # when the flattened transcript is not.
    turns = []
    for entry in artifact.get("messages") or message.get("messages") or []:
        role = entry.get("role")
        content = (entry.get("message") or entry.get("content") or "").strip()
        if role in {"user", "bot", "assistant"} and content:
            speaker = "Caller" if role == "user" else "Aria"
            turns.append(f"{speaker}: {content}")
    return "\n".join(turns)


def duration_from(message: dict[str, Any]) -> float | None:
    value = message.get("durationSeconds") or message.get("duration")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

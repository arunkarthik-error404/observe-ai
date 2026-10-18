"""Reading Vapi's webhook payloads.

Kept apart from the route handlers because this is where a provider's shape
leaks into the system: message envelopes differ between versions, tool calls
arrive in two shapes, and arguments sometimes come as a JSON string. Isolating
it means a change at Vapi's end touches one file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
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


def intent_from(analysis: dict[str, Any], structured: dict[str, Any]) -> str:
    """Read both my schema and the default one Vapi substitutes in squad mode.

    Same failure as `sentiment_from`: a call where no server tool ran leaves no
    session to read intents off, and `structuredData["intent"]` is only there
    when Vapi honoured the configured schema. On the squad it did not, sending
    `call_type` and `issue_type` instead, so the field landed empty on exactly
    the calls with the least other information about them.

    Ordered most specific first — `call_type` is usually something generic like
    "Claims Support" and is the last resort rather than the answer.
    """
    for key in ("intent", "primary_intent", "issue_type", "call_reason", "call_type"):
        value = structured.get(key)
        if value:
            # Matched to the session's own format ("claim_status, faq") so both
            # sources group together in a report instead of splitting in two.
            return " ".join(str(value).lower().split()).replace(" ", "_")
    return ""


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


def response_latencies(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-turn latency as the caller experienced it.

    The number that matters on a voice call is the gap between the caller
    finishing a sentence and the agent starting to speak. Vapi's transcript
    messages carry `time` and `endTime` in epoch milliseconds, so that gap is
    `bot.time - user.endTime` — it spans endpointing, STT finalisation, the
    model, any tool round trip, and TTS time-to-first-byte.

    `tool` marks the turns where a tool ran in that gap, which is what makes
    this actionable: our own tool timings are already measured server-side, so
    a slow turn can be attributed to us or to the voice stack rather than
    guessed at.
    """
    entries = message.get("artifact", {}).get("messages") or message.get("messages") or []
    turns: list[dict[str, Any]] = []
    pending_end: float | None = None
    tool_between = False

    for entry in entries:
        role = entry.get("role")
        if role == "user":
            pending_end = _ms(entry.get("endTime"))
            tool_between = False
        elif role in {"tool_calls", "tool_call_result"}:
            tool_between = True
        elif role == "bot" and pending_end is not None:
            started = _ms(entry.get("time"))
            if started is not None and started > pending_end:
                turns.append({
                    "at_seconds": round(float(entry.get("secondsFromStart") or 0), 2),
                    "latency_ms": round(started - pending_end),
                    "tool": tool_between,
                })
            pending_end = None
            tool_between = False
    return turns


# Below this, a median is one or two turns and the difference between two
# medians is noise. The tool-vs-no-tool comparison is withheld rather than
# shown with a caveat, because a number on a dashboard gets quoted.
MIN_TURNS_TO_COMPARE = 4


@lru_cache
def endpointing_floor_ms() -> int:
    """The wait a turn cannot be faster than, read from the agent config.

    Reported alongside the percentiles because without it the percentiles are
    unreadable: p50 of 2220ms looks like a slow agent until you know 2200ms of
    it is a pause this project deliberately bought. `onNoPunctuationSeconds` is
    the floor for an ordinary turn — digits get a shorter one, a clean sentence
    end shorter still, so this is the common case and not the minimum.

    Returns 0 if the config is not on disk, and the console then omits it
    rather than showing a floor it cannot stand behind.
    """
    path = Path(__file__).resolve().parents[2] / "vapi" / "agents.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))["defaults"]["startSpeakingPlan"]
        wait = float(plan.get("waitSeconds") or 0)
        no_punct = float(plan["transcriptionEndpointingPlan"]["onNoPunctuationSeconds"])
    except Exception:  # noqa: BLE001 - a missing config must not break a webhook
        return 0
    return round((wait + no_punct) * 1000)


def latency_summary(message: dict[str, Any]) -> dict[str, Any]:
    """p50/p95 over a call, against the floor they should be read relative to.

    Small sample, so the percentiles are indicative — the max is usually the
    one worth looking at.
    """
    turns = response_latencies(message)
    if not turns:
        return {}
    values = sorted(t["latency_ms"] for t in turns)
    tool_turns = sorted(t["latency_ms"] for t in turns if t["tool"])
    plain = sorted(t["latency_ms"] for t in turns if not t["tool"])
    # Both sides need enough turns to have a median worth subtracting. A live
    # call compared two tool turns against seven without and produced a
    # -1260ms "tool overhead" that was pure sampling noise.
    comparable = len(tool_turns) >= MIN_TURNS_TO_COMPARE and len(plain) >= MIN_TURNS_TO_COMPARE
    return {
        "turns": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": values[-1],
        "floor_ms": endpointing_floor_ms(),
        "with_tool_ms": _percentile(tool_turns, 0.50) if comparable else None,
        "without_tool_ms": _percentile(plain, 0.50) if comparable else None,
        "tool_turns": len(tool_turns),
        "plain_turns": len(plain),
    }


def _percentile(sorted_values: list[int], fraction: float) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _ms(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def duration_from(message: dict[str, Any]) -> float | None:
    value = message.get("durationSeconds") or message.get("duration")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

"""The tools the voice agent can call, and the dispatcher that runs them.

The registry is written out by hand rather than auto-discovered: the set of
capabilities an agent has is a security decision, so it should be a list
someone can read, not a side effect of which files happen to exist.

See `common.py` for the policy every tool follows.
"""

from __future__ import annotations

import time
from typing import Any

from ..session import CallSession, ToolEvent
from .claims import get_claim_status, get_document_instructions
from .common import EMERGENCY_PATTERNS, EMERGENCY_SCRIPT, ToolHandler, log
from .escalation import escalate_to_human, report_emergency
from .faq import search_knowledge_base
from .identity import lookup_customer, verify_identity
from .interactions import WRITER, log_interaction

__all__ = ["REGISTRY", "dispatch", "WRITER", "EMERGENCY_SCRIPT", "EMERGENCY_PATTERNS"]

REGISTRY: dict[str, ToolHandler] = {
    "lookup_customer": lookup_customer,
    "verify_identity": verify_identity,
    "get_claim_status": get_claim_status,
    "get_document_instructions": get_document_instructions,
    "search_knowledge_base": search_knowledge_base,
    "escalate_to_human": escalate_to_human,
    "report_emergency": report_emergency,
    "log_interaction": log_interaction,
}


def _scan_for_emergency(args: dict[str, Any]) -> str | None:
    for value in args.values():
        if isinstance(value, str):
            match = EMERGENCY_PATTERNS.search(value)
            if match:
                return match.group(0)
    return None


def dispatch(session: CallSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool with safety scanning, timing and timeline capture."""
    handler = REGISTRY.get(name)
    started = time.perf_counter()

    if handler is None:
        result: dict[str, Any] = {
            "ok": False,
            "error": "unknown_tool",
            "tool": name,
            "instruction": "That capability does not exist. Offer a representative instead.",
        }
    else:
        try:
            result = handler(session, args or {})
        except Exception as exc:  # noqa: BLE001 - never let a bug kill the call
            log.exception("tool_crashed", extra={"call_id": session.call_id, "tool": name})
            result = {
                "ok": False,
                "error": "tool_error",
                "detail": str(exc)[:200],
                "instruction": (
                    "Something went wrong on our side. Apologise once, do not explain the technical "
                    "detail, and offer a representative."
                ),
            }

    # Defence in depth: even if the model routed a crisis to the wrong tool,
    # the safety notice rides back on whatever result it does get.
    trigger = _scan_for_emergency(args or {})
    if trigger and name != "report_emergency":
        session.emergency = True
        result = dict(result)
        result["safety_override"] = {
            "trigger": trigger,
            "say_verbatim": EMERGENCY_SCRIPT,
            "instruction": "Possible emergency detected. Say say_verbatim now, before anything else.",
        }

    duration_ms = (time.perf_counter() - started) * 1000
    session.tool_events.append(
        ToolEvent(
            name=name,
            arguments=args or {},
            result=result,
            duration_ms=duration_ms,
            error=None if result.get("ok") else str(result.get("error", "")),
        )
    )
    log.info(
        "tool_call",
        extra={
            "call_id": session.call_id,
            "tool": name,
            "ok": result.get("ok"),
            "duration_ms": round(duration_ms, 1),
        },
    )
    return result

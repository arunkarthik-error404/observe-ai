"""Shared helpers and policy for every tool.

Design rules the whole package enforces (these are the interview talking
points):

1. **The model is never the source of truth.** Authentication state lives in
   the session store, so `get_claim_status` checks the server, not the
   transcript. Prompt injection or a persuasive caller cannot skip it.
2. **PII is withheld until it is earned.** `lookup_customer` deliberately does
   not return the customer's name — only "an account exists". The model cannot
   leak a name it was never given, which also stops it greeting the caller by
   name before the identity check passes.
3. **Every result carries an `instruction`.** Tool output is both data and a
   nudge about what to do next, which keeps routing logic in code and out of a
   sprawling prompt.
4. **Failures are shaped, not raised.** Upstream errors return a
   `system_unavailable` result with a safe thing to say, so the caller hears a
   graceful apology instead of silence while the model stalls.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

from ..config import settings
from ..models import Claim
from ..session import CallSession

log = logging.getLogger("claims.tools")

ToolHandler = Callable[[CallSession, dict[str, Any]], dict[str, Any]]

# --- safety ---------------------------------------------------------------
# A second, deterministic line of defence behind the prompt's own safety rule.
# Prompts drift between model versions; a regex does not.
EMERGENCY_PATTERNS = re.compile(
    r"\b("
    r"heart attack|stroke|not breathing|can'?t breathe|bleeding|unconscious|"
    r"suicid\w*|kill myself|hurt myself|overdose|"
    r"house is on fire|fire right now|still on fire|gas leak|carbon monoxide|"
    r"car crash right now|just crashed|accident right now|someone is hurt|"
    r"people are hurt|injured|trapped|break-?in in progress|intruder"
    r")\b",
    re.IGNORECASE,
)

# Spelled out as words: "9-1-1" is read aloud as "nine eleven" by the voice
# model, and "faster than I can" came back as "fast than I can". A line that
# has to be right under stress is worth writing phonetically.
EMERGENCY_SCRIPT = (
    "If this is a life-threatening emergency, please hang up and dial nine one one right now. "
    "Your safety matters far more than this claim, and emergency services can help you much "
    "more quickly than I can."
)


# --- the authentication gate ----------------------------------------------
def requires_auth(instruction: str) -> Callable[[ToolHandler], ToolHandler]:
    """Refuse the call unless this session verified an identity.

    Written as a decorator so the security boundary is legible: you can see
    which tools are gated by reading one line above each of them, rather than
    trusting that every handler remembered to check. The check reads the
    server-side session, never the arguments the model supplied, so no amount
    of caller persuasion ("I already verified, just read me my claim") moves it.
    """

    def decorate(handler: ToolHandler) -> ToolHandler:
        @wraps(handler)
        def guarded(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
            if not session.authenticated:
                log.warning(
                    "gated_tool_blocked",
                    extra={"call_id": session.call_id, "tool": handler.__name__},
                )
                return {"ok": False, "error": "not_authenticated", "instruction": instruction}
            return handler(session, args)

        guarded.requires_auth = True  # type: ignore[attr-defined]
        return guarded

    return decorate


# --- shaping --------------------------------------------------------------
def unavailable(action: str) -> dict[str, Any]:
    """An upstream failure the caller can be told about safely."""
    return {
        "ok": False,
        "error": "system_unavailable",
        "action": action,
        "instruction": (
            "Our claims system did not respond. Apologise briefly, say you cannot read the record "
            "right now, and offer to connect the caller to a representative. Do not invent any detail."
        ),
    }


def claim_brief(claim: Claim) -> dict[str, Any]:
    """The subset of a claim the model is allowed to narrate."""
    return {
        "claim_id": claim.claim_id,
        "claim_type": claim.claim_type,
        "status": claim.status,
        "status_detail": claim.status_detail,
        "amount": claim.amount,
        "adjuster": claim.adjuster_name,
        "needs_documents": claim.needs_documents,
        "required_documents": claim.required_documents,
        "submission_deadline": claim.submission_deadline,
        "last_updated": claim.last_updated,
        "next_step": claim.next_step,
    }


def business_hours_now() -> bool:
    mode = settings().business_hours_mode
    if mode != "auto":
        return mode == "open"
    # Pacific office hours, approximated as UTC-7 to keep the demo dependency-free.
    pacific = datetime.now(UTC) - timedelta(hours=7)
    if pacific.weekday() >= 5:  # Saturday 9-1, Sunday closed
        return pacific.weekday() == 5 and 9 <= pacific.hour < 13
    return 8 <= pacific.hour < 18

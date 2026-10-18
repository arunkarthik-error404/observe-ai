"""Post-call audit: which spoken rules the model actually kept.

Every rule this checks is already in the system prompt, stated explicitly, with
worked examples of the wrong form. Two live calls broke four of them anyway —
the name used before verification, a lookup outcome announced while the lookup
was still running, "Hold on a sec" in front of a hang-up. The prompt was not
missing the rule; it was 17,459 characters long and the model dropped the parts
that mattered.

That is the case for auditing instead of writing the rule a third time. A
prompt rule is a request, and a request has no failure signal: nothing breaks
when it is ignored, the call sounds fine, and the only evidence is in a
transcript nobody reads. These checks turn each one into a number per call, so
the next prompt change can be read as a trade rather than hoped about.

Deliberately after the fact. The server never sees speech mid-call — by the
time a webhook carries the transcript the caller has hung up — so this cannot
prevent anything. What it can do is say how often prevention failed, which is
what the thresholds and the prompt-trimming work need to aim at.

Checks, and the live failure each one came from:

* `name_before_verification` — "Thank you, Daniel" at 39.9s on a call where
  `verify_identity` was not called until 43.2s and returned the name at 44.8s.
  The name was in no prompt and no earlier tool result. The security shape of
  this is the point: naming a caller confirms the account to someone who has
  not yet proved they own it, and on a real call the name would as easily be
  wrong.
* `outcome_before_result` — "I found an account with that number" spoken 2.8s
  before `lookup_customer` returned. Right by luck. On the not-found scenario
  the same sentence announces an account that does not exist.
* `filler` — the phrases the prompt lists as Wrong, verbatim, said in both
  calls.
* `figures_for_a_voice` — the claim tools send amounts and dates as words
  precisely so the model never sees a digit form to copy. A digit form in the
  agent's mouth therefore means it converted one back.
* `goodbye_before_signoff` — the sign-off is Vapi's `endCallMessage`. A
  farewell from the model stacks in front of it and the caller hears two.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SPEECH_ROLES = {"bot", "assistant"}

# From the prompt's own "Wrong:" list. Matched on the agent's words only, so a
# caller saying "hold on" is not a finding.
FILLERS = re.compile(
    r"\b(?:just a (?:sec|second|moment)|hold on(?: a (?:sec|second))?|one moment"
    r"|give me a (?:sec|second|moment)|bear with me|this will (?:just )?take a sec)\b",
    re.IGNORECASE,
)

# "I found an account", "there's no account", "I can't find an account" — any
# claim about what a lookup will say, in the window before it has said it.
LOOKUP_OUTCOME = re.compile(
    r"\b(?:i (?:have |can )?(?:found|see|located)\b|there(?:'s| is| are)\b"
    r"|i (?:can(?:'?t| not)|could(?:n'?t| not)) (?:find|see|locate)\b"
    r"|no account\b|don'?t have an account\b)",
    re.IGNORECASE,
)

# A bare figure where the tools supplied words. Money with a currency symbol or
# a "dollars" suffix, and numeric dates in either order.
FIGURES = re.compile(
    r"(?:\$\s?\d|\b\d[\d,]*(?:\.\d\d)?\s*dollars?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:January|February|March|April|May|June|July"
    r"|August|September|October|November|December)\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October|November"
    r"|December)\s+\d{1,2}(?:st|nd|rd|th)?\b)",
    re.IGNORECASE,
)

GOODBYE = re.compile(
    r"\b(?:goodbye|bye now|take care|have a (?:great|good|nice) (?:day|evening)"
    r"|thanks for calling|thank you for calling)\b",
    re.IGNORECASE,
)

# Reference numbers are read a digit at a time and dates are spoken as words, so
# a run of digits in the agent's mouth is only ever a date or an amount. Claim
# ids are exempt: "CLM-48201" is a legitimate thing to read out when asked.
_CLAIM_ID = re.compile(r"\b(?:CLM|POL)[-\s]?\d+", re.IGNORECASE)


def _normalise(line: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", line.lower()).strip()


@lru_cache
def automatic_lines() -> frozenset[str]:
    """The lines Vapi speaks on its own, which must not be audited as speech.

    The greeting and the sign-off are configuration, not model output, and both
    arrive in the transcript as ordinary agent turns. Without this the greeting
    trips the farewell check on literally every call ("Thanks for calling
    Observe Insurance claims support…") and the sign-off trips it again at the
    end — two findings per call that no prompt change could ever clear, which
    is the fastest way to make an audit ignorable.
    """
    path = Path(__file__).resolve().parents[1] / "vapi" / "agents.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing config must not break a webhook
        return frozenset()

    lines = {config.get("defaults", {}).get("endCallMessage", "")}
    for assistant in (config.get("assistants") or {}).values():
        if isinstance(assistant, dict):
            lines.add(assistant.get("firstMessage") or "")
            lines.add(assistant.get("endCallMessage") or "")
    return frozenset(_normalise(line) for line in lines if line)


def _is_automatic(said: str) -> bool:
    """True when this turn is the configured greeting or sign-off.

    Compared loosely because what lands in the transcript is a transcription of
    the synthesised audio, not the configured string: the same greeting came
    back verbatim on one call and as "Observe Claims Support … How can can I
    help? You today?" on the next. An exact match would have caught the first
    and missed the second.
    """
    spoken = _normalise(said)
    if not spoken:
        return False
    for line in automatic_lines():
        if spoken == line or spoken in line or line in spoken:
            return True
        # Transcription drift: same words, a few of them mangled or dropped.
        words, expected = set(spoken.split()), set(line.split())
        if expected and len(words & expected) / len(expected) >= 0.7:
            return True
    return False


def _at(entry: dict[str, Any]) -> float:
    try:
        return round(float(entry.get("secondsFromStart") or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _result_of(entry: dict[str, Any]) -> dict[str, Any]:
    """Tool results arrive as an object or as a JSON string, depending on shape."""
    raw = entry.get("result")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _finding(kind: str, entry: dict[str, Any], detail: str, quote: str) -> dict[str, Any]:
    return {
        "check": kind,
        "at_seconds": _at(entry),
        "detail": detail,
        # Enough to find the moment in the recording, not the whole turn.
        "said": quote.strip()[:160],
    }


def audit_call(entries: list[dict[str, Any]], verified_names: set[str]) -> list[dict[str, Any]]:
    """Walk one call's messages and report the spoken rules it broke.

    `entries` is Vapi's message list, in order. `verified_names` is every name
    the server handed over for this caller — the check is whether one was said
    before the tool that supplied it answered, so an empty set means the name
    check cannot fire and the rest still do.
    """
    findings: list[dict[str, Any]] = []

    # Two passes, because the question each check asks is about a moment
    # relative to a gate that closes later in the list. A single pass judged
    # every early turn against a gate it had not read yet, and reported a
    # verified call as one where verification never happened.
    verified_at: float | None = None
    lookup_issued_at: float | None = None
    lookup_answered_at: float | None = None

    for entry in entries:
        role = entry.get("role")
        if role == "tool_calls":
            for call in entry.get("toolCalls") or []:
                name = (call.get("function") or {}).get("name") or call.get("name")
                if name == "lookup_customer" and lookup_issued_at is None:
                    lookup_issued_at = _at(entry)
        elif role == "tool_call_result":
            name = entry.get("name") or ""
            if name == "verify_identity" and verified_at is None and _result_of(entry).get("verified"):
                verified_at = _at(entry)
            elif name == "lookup_customer" and lookup_answered_at is None:
                lookup_answered_at = _at(entry)

    names = {n.strip().lower() for n in verified_names if n and n.strip()}
    # Match whole words only: a surname inside another word is not a disclosure.
    name_patterns = [
        (name, re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)) for name in sorted(names)
    ]

    for entry in entries:
        if entry.get("role") not in SPEECH_ROLES:
            continue

        said = (entry.get("message") or entry.get("content") or "").strip()
        if not said or _is_automatic(said):
            continue
        at = _at(entry)

        # 1. The caller's name, before the tool that supplies it has returned it.
        if verified_at is None or at < verified_at:
            for name, pattern in name_patterns:
                if pattern.search(said):
                    when = ("verification never succeeded in this call"
                            if verified_at is None
                            else f"verify_identity returned it at {verified_at}s")
                    findings.append(_finding(
                        "name_before_verification", entry,
                        f"said {name.title()!r} at {at}s; {when}", said))
                    break

        # 2. A lookup outcome asserted while the lookup is still in flight. The
        #    request-start line already covers that gap, so this is the model
        #    filling a silence that was not silent. The usual mechanism is one
        #    completion carrying both the tool call and a sentence about what it
        #    is going to say.
        in_flight = (
            lookup_issued_at is not None
            and at >= lookup_issued_at
            and (lookup_answered_at is None or at < lookup_answered_at)
        )
        if in_flight:
            match = LOOKUP_OUTCOME.search(said)
            if match:
                findings.append(_finding(
                    "outcome_before_result", entry,
                    f"asserted {match.group(0)!r} while lookup_customer was still running "
                    f"(issued {lookup_issued_at}s)", said))

        # 3. The filler phrases the prompt lists as Wrong.
        for match in FILLERS.finditer(said):
            findings.append(_finding("filler", entry, f"said {match.group(0)!r}", said))

        # 4. Figures where the tools supplied words.
        stripped = _CLAIM_ID.sub("", said)
        match = FIGURES.search(stripped)
        if match:
            findings.append(_finding(
                "figures_for_a_voice", entry,
                f"said {match.group(0)!r} as figures; the tools send words", said))

        # 5. A farewell in front of the one that plays automatically.
        match = GOODBYE.search(said)
        if match:
            findings.append(_finding(
                "goodbye_before_signoff", entry,
                f"said {match.group(0)!r}; endCallMessage speaks the sign-off", said))

    return findings


def audit_summary(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Counts per check, for the one log line and the console."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["check"]] = counts.get(finding["check"], 0) + 1
    return counts

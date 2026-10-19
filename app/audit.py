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

# A claim about whether the account exists, made before the lookup has said.
# Deliberately requires the word account/record/policy near the verb: an
# earlier, looser version matched any "there are", which flagged the knowledge
# base's own "There are four ways to submit documents" on calls where no
# lookup ever ran.
LOOKUP_OUTCOME = re.compile(
    r"\b(?:"
    r"(?:i )?(?:have |can )?(?:found|see|see that|located|got|have)\s+"
    r"(?:an?|your|that)\s+(?:account|record|policy)"
    r"|there(?:'s| is| are)\s+(?:an?|no)\s+(?:account|record|policy)"
    r"|(?:can(?:'?t| not)|could(?:n'?t| not)|unable to)\s+(?:find|see|locate)\s+"
    r"(?:an?|your|that)?\s*(?:account|record|policy)"
    r"|no account\b|don'?t have an account\b"
    r")",
    re.IGNORECASE,
)

# The emergency script must be the next thing the caller hears, and then the
# call must end so they can dial out. Both halves failed on one live call.
#
# Tolerant of a fragmented delivery on purpose. The first version required the
# digits contiguous — `dial 9\s?1\s?1` — and the one live call it was written
# for split them across three transcript segments: "dial 9", then "1", then
# "1 1 right now". The script went out in pieces and the safety check went
# quiet, which is the worst way for this particular check to fail.
EMERGENCY_SCRIPT_HINT = re.compile(
    r"\b(?:hang up and dial|dial 9|call 9\s?1\s?1|emergency service|life.?threatening)",
    re.IGNORECASE,
)

# A filler this far before the emergency tool answered still counts as being
# in front of the script: the model emits the filler and the tool call in one
# completion, so the filler is spoken while the tool is in flight.
EMERGENCY_LEAD_SECONDS = 15.0

# A turn long enough that the caller stops listening. "One or two sentences
# per turn, then stop and listen" is the prompt's first rule about speech, and
# a live call answered with 453 characters — about twenty-five seconds of
# uninterrupted speech — then asked a question the caller took twenty seconds
# to answer, because they had just been talked at.
#
# 400 characters rather than a sentence count: ASR punctuation is unreliable,
# and what matters is seconds of audio, which tracks length closely.
MAX_TURN_CHARS = 400

# How long the line may stay open after the emergency script before it counts
# as having failed to end. Generous on purpose: the script itself takes a few
# seconds to speak, and a caller who says they are safe and wants to carry on
# is a legitimate reason to stay — the prompt allows it. Thirty seconds is
# past both.
EMERGENCY_GRACE_SECONDS = 30.0

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
        # Equal, or the transcript caught only part of it.
        if spoken == line or spoken in line:
            return True
        # Transcription drift: same words, a few of them mangled or dropped.
        #
        # Similarity both ways, not recall against the configured line. A
        # one-sided test asked only "does this contain the sign-off's words",
        # so a turn that contained the sign-off AND something else scored 6/7
        # and was waved through whole: a live call closed with "This will just
        # take a sec. Thanks for calling Observe Insurance." and the audit
        # reported neither the forbidden filler nor the model's own farewell.
        words, expected = set(spoken.split()), set(line.split())
        union = words | expected
        if union and len(words & expected) / len(union) >= 0.7:
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
    emergency_at: float | None = None
    end_call_at: float | None = None
    last_speech_at: float = 0.0

    for entry in entries:
        role = entry.get("role")
        if role == "tool_calls":
            for call in entry.get("toolCalls") or []:
                name = (call.get("function") or {}).get("name") or call.get("name")
                if name == "lookup_customer" and lookup_issued_at is None:
                    lookup_issued_at = _at(entry)
                elif name == "endCall" and end_call_at is None:
                    end_call_at = _at(entry)
        elif role == "tool_call_result":
            name = entry.get("name") or ""
            if name == "verify_identity" and verified_at is None and _result_of(entry).get("verified"):
                verified_at = _at(entry)
            elif name == "lookup_customer" and lookup_answered_at is None:
                lookup_answered_at = _at(entry)
            elif name == "report_emergency" and emergency_at is None:
                emergency_at = _at(entry)
        elif role in SPEECH_ROLES and (entry.get("message") or "").strip():
            last_speech_at = max(last_speech_at, _at(entry))

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

        # 2. The account's existence asserted before the lookup has said so.
        #
        #    The window is everything before the RESULT, not the gap between
        #    the call and the result. An earlier version checked only that gap
        #    and scored two live calls clean: both announced "I found an
        #    account" eight to ten seconds before `lookup_customer` was even
        #    issued, so the model was describing a lookup it had not started.
        #    Sitting outside the window made the worse version of the bug the
        #    one the audit could not see.
        before_answer = lookup_answered_at is None or at < lookup_answered_at
        if before_answer:
            match = LOOKUP_OUTCOME.search(said)
            if match:
                when = ("lookup_customer was never called"
                        if lookup_issued_at is None
                        else f"it was issued at {lookup_issued_at}s and answered at "
                             f"{lookup_answered_at}s")
                findings.append(_finding(
                    "outcome_before_result", entry,
                    f"asserted {match.group(0)!r} at {at}s; {when}", said))

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

        # 5. A filler in front of the emergency script.
        #
        #    `report_emergency` returns `say_verbatim` and an instruction that
        #    names this exact failure — "with nothing before it, no 'just a
        #    sec', no filler". A live call answered a caller whose kitchen was
        #    on fire with "Just a sec. If this is a life threatening emergency,
        #    please hang up and dial 911". Scored as an ordinary filler it is
        #    one of five; it is not one of five.
        #    Two ways in, because either one alone missed a live call: the
        #    utterance looks like the script, OR it sits in the window around
        #    the moment `report_emergency` answered.
        near_emergency = (
            emergency_at is not None and at >= emergency_at - EMERGENCY_LEAD_SECONDS
        )
        if EMERGENCY_SCRIPT_HINT.search(said) or near_emergency:
            cut = said.upper().find("IF THIS")
            lead = said[: cut if cut > 0 else 40]
            match = FILLERS.search(lead) or (FILLERS.search(said) if near_emergency else None)
            if match:
                findings.append(_finding(
                    "filler_before_emergency", entry,
                    f"said {match.group(0)!r} in front of the 911 script", said))

        # 6. Stacked questions — the prompt asks for one at a time.
        if said.count("?") > 1:
            findings.append(_finding(
                "stacked_questions", entry,
                f"{said.count('?')} questions in one turn", said))

        # 7. A turn the caller cannot be expected to hold. The emergency script
        #    is exempt: it is `say_verbatim` and its length is deliberate.
        if len(said) > MAX_TURN_CHARS and not EMERGENCY_SCRIPT_HINT.search(said):
            findings.append(_finding(
                "turn_too_long", entry,
                f"{len(said)} characters in one turn, roughly "
                f"{round(len(said) / 18)}s of speech", said))

        # 8. A farewell in front of the one that plays automatically.
        #
        #    Only before `endCall` is issued. After it, the configured sign-off
        #    is playing, and the transcriber merges it into whatever segment
        #    the agent was already in — so a farewell found there is usually
        #    the configured one being read back, not the model writing its own.
        #    Before `endCall` the sign-off cannot have played yet, so a
        #    farewell is the model's by construction.
        after_hangup = end_call_at is not None and at > end_call_at
        match = None if after_hangup else GOODBYE.search(said)
        if match:
            findings.append(_finding(
                "goodbye_before_signoff", entry,
                f"said {match.group(0)!r}; endCallMessage speaks the sign-off", said))

    # 9. The call outliving the emergency that should have ended it.
    #
    #    A caller cannot dial emergency services while they are still
    #    connected, so `report_emergency` tells the model to say the script
    #    and end the call. On a live call it said the script and then held the
    #    line for thirty-two seconds, long enough for the configured idle
    #    message — "I'm still here whenever you're ready" — to play at someone
    #    whose kitchen was on fire.
    if emergency_at is not None and last_speech_at - emergency_at > EMERGENCY_GRACE_SECONDS:
        findings.append({
            "check": "emergency_call_not_ended",
            "at_seconds": round(last_speech_at, 2),
            "detail": (f"the line was still open {round(last_speech_at - emergency_at, 1)}s "
                       f"after report_emergency answered at {emergency_at}s"),
            "said": "",
        })

    return findings


def audit_summary(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Counts per check, for the one log line and the console."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["check"]] = counts.get(finding["check"], 0) + 1
    return counts

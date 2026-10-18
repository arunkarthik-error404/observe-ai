"""Per-call state.

The LLM is treated as an untrusted narrator: it decides *what to say*, never
*what is true*. Authentication status, attempt counts and the verified
customer identity live here, server-side, keyed by Vapi's call id. Gated tools
read this store rather than believing arguments the model supplies, so no
amount of caller persuasion ("I already verified, just read me the claim")
can skip the identity check.

In-process dict + TTL sweep is deliberate for a single-instance demo; the
interface is narrow enough that swapping in Redis is a two-method change.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .models import Customer


@dataclass
class ToolEvent:
    """One tool invocation, retained for the live debug timeline."""

    name: str
    arguments: dict[str, Any]
    result: Any
    duration_ms: float
    at: float = field(default_factory=time.time)
    error: str | None = None
    # Which squad member made the call. `assistant_path` only records the order
    # agents first appear, which is not enough to answer "who dropped that?" —
    # diagnosing a verification confirmation lost at a handoff meant leaving
    # this console and reading the provider's own call record to find out.
    assistant: str = ""


@dataclass
class CallSession:
    call_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # authentication
    claimed_phone: str = ""
    candidate: Customer | None = None   # found by phone, not yet verified
    customer: Customer | None = None    # verified identity
    auth_attempts: int = 0
    auth_outcome: str = "pending"  # pending | verified | failed | not_found

    # conversation outcome
    intents: list[str] = field(default_factory=list)
    claims_discussed: list[str] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str = ""
    ticket_id: str = ""
    # Where a human should ring. Deliberately not `claimed_phone`: a caller who
    # cannot remember the number on the policy can still give one to be reached
    # on, and a web call carries no caller ID to fall back to.
    callback_number: str = ""
    # The outcome the agent logged mid-call, remembered so the end-of-call
    # report cannot replace a real outcome with the initial auth state.
    outcome: str = ""
    emergency: bool = False
    tool_events: list[ToolEvent] = field(default_factory=list)
    assistant_path: list[str] = field(default_factory=list)
    # The agent handling the current tool batch, so each event can be attributed.
    current_assistant: str = ""
    # Who was holding the call when identity was confirmed, and whether the
    # caller has yet to be told. In squad mode the agent that runs
    # `verify_identity` hands off within a second of the result, so the
    # instruction to greet the caller by name reaches an agent that is about to
    # stop speaking. See `claims.greeting_note`.
    verified_by: str = ""
    greeting_owed: bool = False
    # Caller-perceived turn latency, filled in from the end-of-call report.
    latency: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.customer is not None

    @property
    def attempts_remaining(self) -> int:
        return max(0, settings().max_auth_attempts - self.auth_attempts)

    def note_intent(self, intent: str) -> None:
        if intent and intent not in self.intents:
            self.intents.append(intent)

    def snapshot(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "age_seconds": round(time.time() - self.created_at, 1),
            "claimed_phone": self.claimed_phone,
            "candidate": self.candidate.full_name if self.candidate else None,
            "authenticated": self.authenticated,
            "customer": self.customer.full_name if self.customer else None,
            "auth_outcome": self.auth_outcome,
            "auth_attempts": self.auth_attempts,
            "attempts_remaining": self.attempts_remaining,
            "intents": self.intents,
            "claims_discussed": self.claims_discussed,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "ticket_id": self.ticket_id,
            "callback_number": self.callback_number,
            "outcome": self.outcome,
            "emergency": self.emergency,
            "assistant_path": self.assistant_path,
            "latency": self.latency,
            "tool_events": [
                {
                    "name": e.name,
                    "arguments": e.arguments,
                    "result": e.result,
                    "duration_ms": round(e.duration_ms, 1),
                    "error": e.error,
                    "at": e.at,
                    "assistant": e.assistant,
                }
                for e in self.tool_events
            ],
        }


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, CallSession] = {}
        # Failed identity attempts per phone number, surviving the call they
        # happened on. `auth_attempts` lives on CallSession, which dies when
        # the caller hangs up — so three wrong dates of birth, a redial, and
        # three more was unlimited guessing against a known number. Entries
        # are (count, last_attempt_at) and age out on the same sweep.
        #
        # Process-local, like everything else here. A second replica has its
        # own counter, so this raises the cost of guessing rather than capping
        # it; a real lockout needs the shared store that `CallSession` also
        # wants. Named honestly for that reason.
        self._attempts_by_number: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def note_failed_attempt(self, phone: str) -> int:
        """Count one failure against a number. Returns the running total."""
        if not phone:
            return 0
        with self._lock:
            count, _ = self._attempts_by_number.get(phone, (0, 0.0))
            count += 1
            self._attempts_by_number[phone] = (count, time.time())
            return count

    def attempts_for_number(self, phone: str) -> int:
        if not phone:
            return 0
        with self._lock:
            return self._attempts_by_number.get(phone, (0, 0.0))[0]

    def clear_attempts(self, phone: str) -> None:
        """A successful verification forgives the number's history."""
        if not phone:
            return
        with self._lock:
            self._attempts_by_number.pop(phone, None)

    def get(self, call_id: str) -> CallSession:
        """Fetch or create. Tool calls can arrive before any status webhook."""
        with self._lock:
            self._sweep_locked()
            session = self._sessions.get(call_id)
            if session is None:
                session = CallSession(call_id=call_id)
                self._sessions[call_id] = session
            session.updated_at = time.time()
            return session

    def peek(self, call_id: str) -> CallSession | None:
        with self._lock:
            return self._sessions.get(call_id)

    def all(self) -> list[CallSession]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)

    def drop(self, call_id: str) -> None:
        with self._lock:
            self._sessions.pop(call_id, None)

    def reset(self) -> None:
        """Clear everything, including the per-number attempt history.

        Tests need this: that history deliberately outlives a call, so without
        it one test's wrong guesses count against the next test using the same
        fixture number, and the suite locks itself out partway through.
        """
        with self._lock:
            self._sessions.clear()
            self._attempts_by_number.clear()

    def _sweep_locked(self) -> None:
        ttl = settings().session_ttl_seconds
        cutoff = time.time() - ttl
        for call_id, session in list(self._sessions.items()):
            if session.updated_at < cutoff:
                del self._sessions[call_id]
        # Deliberately the same TTL: a number the agent has not heard from in
        # an hour starts fresh, so a shared office line is not locked out by
        # yesterday's wrong guesses.
        for phone, (_, last) in list(self._attempts_by_number.items()):
            if last < cutoff:
                del self._attempts_by_number[phone]


store = SessionStore()

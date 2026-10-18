"""Integration #2, in-call path: the post-call record written during the call."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..models import InteractionRecord
from ..repositories import RepositoryError, repository
from ..session import CallSession
from .common import log

# Off-call work that must not block a caller's turn. Small on purpose: if the
# upstream system is slow enough to saturate four workers, the queue depth is
# the signal, not something to paper over with more threads.
WRITER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="interaction-writer")


def log_interaction(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    """Write the interaction record from inside the call.

    The webhook is the primary write-back, but a model-driven call here means
    the record exists even if the caller hangs up mid-sentence and the
    end-of-call report is delayed or dropped. Both paths upsert on call_id, so
    one call is always one row.

    The write is handed to a worker thread rather than awaited: a Sheets append
    measured ~1.4s live, and this tool fires while the caller is still on the
    line waiting to be told goodbye. Nothing the caller hears depends on the
    write succeeding, so it must not sit in conversational latency. Failures
    are logged, and the end-of-call-report webhook — which no caller is waiting
    on, and which Vapi retries — is the backstop.
    """
    # Remembered on the session so the end-of-call report, which has no idea
    # what the agent concluded, cannot overwrite a real outcome with the
    # initial auth state. Only a model-supplied outcome counts — the
    # `auth_outcome` fallback below is a default, not a conclusion.
    supplied_outcome = (args.get("outcome") or "").strip()
    if supplied_outcome:
        session.outcome = supplied_outcome

    record = InteractionRecord(
        call_id=session.call_id,
        caller_name=(session.customer.full_name if session.customer
                     else args.get("caller_name") or "Unverified caller"),
        caller_phone=session.claimed_phone,
        callback_number=session.callback_number,
        summary=(args.get("summary") or "").strip(),
        sentiment=(args.get("sentiment") or "neutral").strip().lower(),
        intent=", ".join(session.intents),
        authenticated=session.authenticated,
        escalated=session.escalated,
        outcome=supplied_outcome or session.auth_outcome,
        claim_ids=", ".join(session.claims_discussed),
        source="in-call-tool",
    )

    def write() -> None:
        try:
            repository().write_interaction(record)
            log.info("interaction_logged", extra={"call_id": record.call_id, "source": record.source})
        except RepositoryError as exc:
            log.error(
                "interaction_write_failed",
                extra={"call_id": record.call_id, "error": str(exc),
                       "dead_letter": record.model_dump()},
            )

    WRITER.submit(write)
    return {
        "ok": True,
        "queued": True,
        "instruction": (
            # This used to end with "ask if there is anything else, and when
            # they say no, end the call", which left two actions outstanding —
            # one of them speech — at the single point in the call where the
            # model is most likely to collapse them. It did, in both
            # directions: one call said "Hold on a sec. Is there anything else
            # I can help you with?", and another issued the hang-up seven
            # tenths of a second after this result, having never asked. The
            # closing question now belongs to the turn *before* this tool, so
            # what is left here is one action and no words.
            "Record saved. The closing question is already behind you. End the call now, and "
            "say nothing at all before you do — no \"just a sec\", no \"one moment\", no "
            "\"give me a moment\", and no goodbye. Nothing is loading and nobody is waiting. "
            # Naming the exact words that will play is what finally stopped the
            # model writing its own farewell: "do not say goodbye" is a rule to
            # remember, whereas this is a sentence it can hear already spoken.
            "The caller will hear \"Thanks for calling Observe Insurance. Take care.\" the "
            "moment you end it — that is the sign-off, it is automatic, and you cannot skip "
            "it or improve on it."
        ),
    }

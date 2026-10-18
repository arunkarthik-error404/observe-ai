"""Knowledge-base lookup.

Ungated on purpose: office hours and a mailing address are not account
specific, so a caller who cannot be identified can still be helped with them.
"""

from __future__ import annotations

from typing import Any

from ..knowledge import MIN_MARGIN, MIN_SCORE, search
from ..session import CallSession
from .claims import greeting_note
from .common import log


def search_knowledge_base(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query") or args.get("question") or ""
    session.note_intent("faq")
    result = search(query)
    result["ok"] = True

    # One structured line per lookup, so the threshold stops being a number
    # someone picked and becomes a number with a distribution behind it. Three
    # questions this answers from real traffic that fixtures cannot: which
    # questions get refused most often (that is the content backlog, ranked by
    # actual demand), whether MIN_SCORE sits in a valley or in the middle of
    # the mass, and which entries never match anything.
    #
    # The query goes through the same redaction filter as everything else; a
    # caller occasionally reads an account number into a question.
    log.info("kb_search", extra={
        "call_id": session.call_id,
        "query": query,
        "outcome": "answered" if result["found"] else result.get("reason", "none"),
        "topic": result.get("topic", ""),
        "best_score": result.get("score", result.get("best_score", 0.0)),
        "margin": result.get("margin"),
        "source": result.get("source", ""),
        "min_score": MIN_SCORE,
        "min_margin": MIN_MARGIN,
    })

    # Which entries the agent actually read from, for the post-call record and
    # the escalation ticket. Closes the loop: a human reviewing a call can
    # check the agent against the document rather than take its word.
    if result.get("source"):
        citation = f"{result['topic']} ({result['source']})"
        if citation not in session.kb_consulted:
            session.kb_consulted.append(citation)

    # A caller who verifies and then asks a general question reaches no claims
    # tool, so without this the confirmation they are owed never gets said.
    # No-op unless one is outstanding.
    result["instruction"] = greeting_note(session) + result["instruction"]
    return result

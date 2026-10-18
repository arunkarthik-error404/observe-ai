"""Knowledge-base lookup.

Ungated on purpose: office hours and a mailing address are not account
specific, so a caller who cannot be identified can still be helped with them.
"""

from __future__ import annotations

from typing import Any

from ..knowledge import search
from ..session import CallSession
from .claims import greeting_note


def search_knowledge_base(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query") or args.get("question") or ""
    session.note_intent("faq")
    result = search(query)
    result["ok"] = True
    # A caller who verifies and then asks a general question reaches no claims
    # tool, so without this the confirmation they are owed never gets said.
    # No-op unless one is outstanding.
    result["instruction"] = greeting_note(session) + result["instruction"]
    return result

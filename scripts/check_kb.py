"""Retrieval harness for the knowledge base.

Two fixtures, two failure modes: questions that must be answered (recall) and
questions that must NOT be (leaks — the agent inventing an answer is worse
than admitting it does not know). Run this after editing faq.md or MIN_SCORE:

    python scripts/check_kb.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import MIN_SCORE, search  # noqa: E402

MUST_ANSWER = [
    ("when are you open", "Office hours"),
    ("are you open on the weekend", "Office hours"),
    ("what are your office hours", "Office hours"),
    ("what's your address", "Mailing address"),
    ("where do I mail my documents", "Mailing address"),
    ("i want to file a new claim how do i do that", "How to start a new claim"),
    ("how do i start a claim", "How to start a new claim"),
    ("what happens after i submit everything", "General claims process"),
    ("how long does the whole claims process take", "General claims process"),
    ("how do I upload my photos", "How to submit documents"),
    ("can i email the paperwork", "How to submit documents"),
    ("how long until i get my money", "Payment timing"),
    ("can I appeal a denial", "Appealing a denied claim"),
    ("i disagree with the decision", "Appealing a denied claim"),
    ("who is handling my case", "Adjuster contact"),
    ("can i get a rental car", "Rental car coverage"),
    ("is this call being recorded", "Privacy and recording"),
    ("how much is my excess", "Deductibles"),
]

MUST_REFUSE = [
    "whats the weather tomorrow",
    "do you sell pet insurance for my dog",
    "can you tell me a joke",
    "what is my credit score",
    "who won the game last night",
    "what do you think about the election",
    "can you write me a poem about claims",
]


def main() -> int:
    failures = 0
    print(f"MIN_SCORE = {MIN_SCORE}\n-- recall --")
    for question, expected in MUST_ANSWER:
        result = search(question)
        ok = result["found"] and result["topic"] == expected
        failures += not ok
        got = result.get("topic") or f"no match (best {result.get('best_score')})"
        print(f"{'PASS' if ok else 'FAIL'}  {question!r:50} -> {got}")

    print("-- must refuse --")
    for question in MUST_REFUSE:
        result = search(question)
        ok = not result["found"]
        failures += not ok
        detail = f"best {result.get('best_score')}" if ok else f"LEAKED {result.get('topic')}"
        print(f"{'PASS' if ok else 'FAIL'}  {question!r:50} -> {detail}")

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

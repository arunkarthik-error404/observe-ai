"""Knowledge-base retrieval.

Deliberately lexical, not vector-based: the corpus is ~12 entries, so
rapidfuzz token-set scoring answers in under a millisecond, adds no network
hop to the latency budget, and is *auditable* — the debug timeline shows the
exact score for every candidate entry, which matters more in a voice call than
semantic nuance. The retrieval interface is the part that would survive a swap
to embeddings + a reranker at corpus scale.

Below `MIN_SCORE` the tool reports `no_confident_match` instead of returning a
weak chunk. That is the single most important line of defence against the agent
inventing an office hour or an address.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from ..models import speakable

KB_PATH = Path(__file__).parent / "faq.md"
MIN_SCORE = 52.0  # tuned against the recall/leak fixtures in scripts/check_kb.py

# How far clear of the runner-up the winner has to be. A single threshold can
# say "weak" but cannot say "confident, and ambiguous" — and that second thing
# is what broke a live call, where "how do I add a photo to an existing claim"
# matched "How to start a new claim" at 55.9 and the agent answered it.
#
# The fixtures are full of ties: "how do i send you the paper work" scores
# 57.8 against both Mailing address and How to submit documents, and "what's
# the status of my claim" scores 43.2 against two entries at once. Picking the
# first of two equals is a coin toss presented to the caller as an answer.
#
# Patched in the prompt first — an instruction telling the model to notice the
# match was wrong. That is the thing this codebase refuses to do everywhere
# else: `requires_auth` does not ask the model to respect the gate, it closes
# it. So the decision moves back to the server, and the fourth outcome is a
# clarifying question, which is what a human agent would ask anyway.
MIN_MARGIN = 8.0

# The written-to-spoken transform lives in `models` alongside `spoken_amount`,
# because the knowledge base is not the only place a screen-shaped string
# reaches the voice — the claim record and the document channels do it too.


@dataclass
class KBEntry:
    title: str
    keywords: list[str]
    answer: str

    @property
    def haystack(self) -> str:
        return f"{self.title} {' '.join(self.keywords)}".lower()


@lru_cache
def entries() -> list[KBEntry]:
    text = KB_PATH.read_text(encoding="utf-8")
    out: list[KBEntry] = []
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        lines = block.strip().splitlines()
        title = lines[0].strip()
        keywords: list[str] = []
        body: list[str] = []
        for line in lines[1:]:
            if line.lower().startswith("keywords:"):
                keywords = [k.strip() for k in line.split(":", 1)[1].split(",") if k.strip()]
            else:
                body.append(line)
        answer = " ".join(" ".join(body).split())
        if answer:
            out.append(KBEntry(title=title, keywords=keywords, answer=answer))
    return out


def search(query: str, top_k: int = 3) -> dict:
    q = (query or "").strip().lower()
    if not q:
        return {
            "found": False,
            "reason": "empty_query",
            "candidates": [],
            "instruction": (
                "No question was passed. Ask the caller to say again what they want to know, then "
                "call this tool with their words. Do not answer from your own knowledge."
            ),
        }

    scored = []
    for entry in entries():
        # token_set_ratio tolerates the extra words STT leaves in a spoken
        # question ("uh so what are your office hours today"). Scoring on the
        # title+keywords haystack rather than per-keyword matters: scoring each
        # short keyword separately made junk queries match everything, because
        # a 4-letter keyword partial-matches almost any sentence.
        score = float(fuzz.token_set_ratio(q, entry.haystack))
        # An exact spoken phrase is strong evidence; the length floor keeps
        # stopword-ish keywords from firing it. Tracked separately as well as
        # folded into the score, because the two are not the same evidence:
        # downstream, a keyword hit at 92 and a fuzzy match at 92 were
        # indistinguishable, and the margin check below needs to tell them
        # apart — two keyword hits tie at 92 and are genuinely ambiguous.
        exact = any(len(kw) >= 6 and kw in q for kw in entry.keywords)
        if exact:
            score = max(score, 92.0)
        scored.append((score, entry, exact))
    scored.sort(key=lambda triple: triple[0], reverse=True)

    candidates = [{"title": e.title, "score": round(float(s), 1), "exact_keyword": x}
                  for s, e, x in scored[:top_k]]
    best_score, best, _ = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    margin = round(float(best_score - runner_up), 1)
    if best_score < MIN_SCORE:
        return {
            "found": False,
            "reason": "no_confident_match",
            "best_score": round(float(best_score), 1),
            "candidates": candidates,
            "instruction": (
                "Nothing in the knowledge base answers that. Do not guess, and do not answer it "
                "from your own knowledge even if you are certain you know it — this tool is the "
                "only approved source for a fact you state out loud. Tell the caller it is not "
                "something you can confirm on this line, and offer either a representative or a "
                "callback."
            ),
        }

    # Confident, but about two things at once. The fourth outcome: ask, the way
    # a person would, instead of picking the first of two equals and presenting
    # a coin toss as an answer.
    if margin < MIN_MARGIN:
        second = scored[1][1]
        return {
            "found": False,
            "reason": "ambiguous",
            "score": round(float(best_score), 1),
            "margin": margin,
            "options": [best.title, second.title],
            "candidates": candidates,
            "instruction": (
                f"Two different topics match this about equally — {best.title!r} and "
                f"{second.title!r} — so there is no way to tell which one the caller meant. "
                "Do not pick one. Ask them which they mean, in one short question, in plain "
                "words rather than these titles: something like \"are you asking about sending "
                "documents in, or about where to post them?\" Then call this tool again with "
                "their answer. If they clarify and it still does not match, say it is not "
                "something you can confirm on this line and offer a representative."
            ),
        }

    return {
        "found": True,
        "topic": best.title,
        "margin": margin,
        # The spoken form is the only one returned, so there is no second
        # string for the model to read out by mistake. faq.md stays the single
        # written source of truth.
        "answer": speakable(best.answer),
        "score": round(float(best_score), 1),
        "candidates": candidates,
        # The third branch, added after a live call fell through the gap between
        # the other two. `MIN_SCORE` only filters *weak* matches; it does nothing
        # about a confident match on the wrong entry. "How to add a photo to an
        # existing claim" scored 55.9 against "How to start a new claim" — well
        # above the threshold and not an answer to the question. Told to use that
        # text and nothing else, the model substituted its own answer instead,
        # which is the one outcome this whole tool exists to prevent. So the
        # near-miss now has an instruction of its own.
        "instruction": (
            "Answer using only the text in 'answer'. Do not add facts. "
            "First check it actually answers what the caller asked: 'topic' is the entry this "
            "matched, and a confident match can still be the wrong topic. If it does not answer "
            "them, do not stretch it to fit and do not substitute an answer of your own — say it "
            "is not something you can confirm on this line and offer a representative, exactly as "
            "you would if nothing had been found. "
            # Reading one of these out in full ran to twenty-three seconds on a
            # live call, and the voice dropped a phone number partway through.
            # A caller who wanted the quickest way to send a photo had to sit
            # through a fax number and a P.O. box to get it.
            "Then cut it down. If the answer lists several options, give the best one or two and "
            "say there are others if they would rather — do not read the whole list. Two or three "
            "sentences, then stop and let them ask. The text is what you may say, not a script you "
            "have to finish."
        ),
    }

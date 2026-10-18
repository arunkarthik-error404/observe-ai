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

KB_PATH = Path(__file__).parent / "faq.md"
MIN_SCORE = 52.0  # tuned against the recall/leak fixtures in scripts/check_kb.py

# Written forms that a text-to-speech voice mangles or silently drops. Same
# problem `spoken_amount` solves for money and the phonetic emergency script
# solves for "9-1-1": the entry is correct on a screen and wrong out loud.
#
# A live call read `claims@observeinsurance.com` as "claims observe insurance
# dot com" — the `@` simply vanished, leaving the caller an address that does
# not exist — and dropped the fax number entirely mid-sentence.
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_URL = re.compile(r"\b([\w-]+(?:\.[\w-]+)+)/(\S+)")
_PHONE = re.compile(r"\b(\d{3})-(\d{3})-(\d{4})\b")
_DOMAIN = re.compile(r"\b([\w-]+)\.(com|org|net)\b")


def speakable(text: str) -> str:
    """Rewrite a knowledge-base answer into something a voice can say.

    Deliberately a transform rather than a second copy of each answer in the
    file. A hand-written spoken variant is one edit away from disagreeing with
    the written one, and a knowledge base whose two halves disagree is worse
    than one that is awkward in places.
    """
    text = _PHONE.sub(
        lambda m: ", ".join(" ".join(group) for group in m.groups()), text)
    text = _EMAIL.sub(
        lambda m: f"{m.group(1)} at {m.group(2).replace('.', ' dot ')}", text)
    text = _URL.sub(
        lambda m: f"{m.group(1).replace('.', ' dot ')} slash {m.group(2)}", text)
    text = _DOMAIN.sub(lambda m: f"{m.group(1)} dot {m.group(2)}", text)
    return " ".join(text.split())


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
        # stopword-ish keywords from firing it.
        if any(len(kw) >= 6 and kw in q for kw in entry.keywords):
            score = max(score, 92.0)
        scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    candidates = [{"title": e.title, "score": round(float(s), 1)} for s, e in scored[:top_k]]
    best_score, best = scored[0]
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
    return {
        "found": True,
        "topic": best.title,
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

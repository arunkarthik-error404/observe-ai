"""Retrieval metrics for the knowledge base.

This printed PASS/FAIL per case, which answers "did anything break" and not
"is it getting better or worse". A threshold change that lifts recall two
points while doubling the leaks passed cleanly. Now it prints numbers, and
compares them against a committed baseline, so a trade is visible as a trade:

    python scripts/check_kb.py                  # measure, compare, exit 1 on regression
    python scripts/check_kb.py --verbose        # list every failing case
    python scripts/check_kb.py --update-baseline

The metrics, and the failure each one catches:

* **recall@1 / recall@3** — is the right entry first, or at least findable.
* **MRR** — catches the right answer slipping from first to second, which
  recall@3 hides entirely.
* **refusal rate** — of the questions this line must not answer, how many it
  refused. The single most important number here: a wrong answer in the
  agent's voice is Observe Insurance getting it wrong.
* **false refusal rate** — answerable questions that got refused. Invisible
  until now, because nothing breaks when it happens: the caller is simply
  told we cannot confirm that, and goes away. The quiet half of the trade
  every threshold change makes.
* **margin** — the gap between the best and second-best entry. A confident
  score on the wrong entry is the failure mode a single threshold cannot
  express, and the margin is what expresses it.

Baselines are floors with tolerance, not exact targets: retrieval numbers
move a little with any content edit, and a harness that cries wolf gets
ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_fixtures import (  # noqa: E402
    HELD_OUT,
    MUST_ANSWER,
    MUST_REFUSE,
    NEAR_MISS,
    STT_NOISE,
)

from app.knowledge import MIN_SCORE, search  # noqa: E402

BASELINE_PATH = Path(__file__).parent / "kb_baseline.json"
# How far a metric may drift before it counts as a regression. Content edits
# move these by a point or two; a real regression moves them further.
TOLERANCE = 0.02


def rank_of(query: str, expected: str) -> tuple[int, dict]:
    """Where the expected entry placed, and the raw result. 0 means unplaced."""
    result = search(query, top_k=5)
    titles = [c["title"] for c in result["candidates"]]
    return (titles.index(expected) + 1 if expected in titles else 0), result


def measure(cases: list[tuple[str, str]], name: str) -> dict:
    """Recall, MRR, false-refusal and margin over one labelled set."""
    if not cases:
        return {}
    ranks, margins, refused, clarified, misses = [], [], 0, 0, []
    for query, expected in cases:
        rank, result = rank_of(query, expected)
        ranks.append(rank)
        scores = [c["score"] for c in result["candidates"]]
        if len(scores) >= 2:
            margins.append(round(scores[0] - scores[1], 1))
        # Being asked which of two topics you meant is not being turned away.
        # Counting them together hid the whole point of the margin check.
        if result.get("reason") == "ambiguous":
            clarified += 1
        elif not result["found"]:
            refused += 1
        if rank != 1:
            misses.append((query, expected, result["candidates"][:2]))
    n = len(cases)
    return {
        "set": name,
        "cases": n,
        "recall@1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "recall@3": round(sum(1 for r in ranks if 1 <= r <= 3) / n, 3),
        "mrr": round(mean(1 / r if r else 0.0 for r in ranks), 3),
        # Answerable questions we turned away. Nothing errors when this rises.
        "false_refusal_rate": round(refused / n, 3),
        # Answerable questions we asked a clarifying question about instead.
        # A cost, but a far smaller one than a refusal or a wrong answer.
        "clarify_rate": round(clarified / n, 3),
        "margin_p50": sorted(margins)[len(margins) // 2] if margins else 0.0,
        "margin_min": min(margins) if margins else 0.0,
        "_misses": misses,
    }


def measure_refusals(queries: list[str]) -> dict:
    refused, clarified, leaked = 0, 0, []
    for query in queries:
        result = search(query, top_k=5)
        if result["found"]:
            leaked.append((query, result["topic"], result["score"]))
        elif result.get("reason") == "ambiguous":
            # Not a leak: no fact is stated. But asking "did you mean A or B?"
            # about a question we cannot answer at all is a worse reply than
            # saying so, which is why it is counted apart from a clean refusal.
            clarified += 1
        else:
            refused += 1
    return {
        "set": "must_refuse",
        "cases": len(queries),
        "refusal_rate": round(refused / len(queries), 3),
        "clarify_rate": round(clarified / len(queries), 3),
        "_leaks": leaked,
    }


def collect() -> dict:
    held_answerable = [(q, t) for q, t in HELD_OUT if t]
    held_refusable = [q for q, t in HELD_OUT if not t]
    return {
        "min_score": MIN_SCORE,
        "sets": [
            measure(MUST_ANSWER, "must_answer"),
            measure(STT_NOISE, "stt_noise"),
            measure(NEAR_MISS, "near_miss"),
            measure_refusals(MUST_REFUSE),
            measure(held_answerable, "held_out"),
            measure_refusals(held_refusable) | {"set": "held_out_refuse"},
        ],
    }


# Metrics where higher is better, and where lower is.
HIGHER = {"recall@1", "recall@3", "mrr", "refusal_rate", "margin_p50", "margin_min"}
LOWER = {"false_refusal_rate", "clarify_rate"}


def compare(current: dict, baseline: dict) -> list[str]:
    """Regressions only. An improvement is reported but never fails."""
    previous = {s["set"]: s for s in baseline.get("sets", [])}
    regressions = []
    for block in current["sets"]:
        old = previous.get(block["set"])
        if not old:
            continue
        for metric, value in block.items():
            if metric.startswith("_") or metric in {"set", "cases"}:
                continue
            was = old.get(metric)
            if not isinstance(was, int | float) or not isinstance(value, int | float):
                continue
            # Margins are points on a 0-100 scale; the rest are fractions.
            slack = TOLERANCE * (100 if metric.startswith("margin") else 1)
            if metric in HIGHER and value < was - slack:
                regressions.append(f"{block['set']}.{metric}: {was} -> {value}")
            if metric in LOWER and value > was + slack:
                regressions.append(f"{block['set']}.{metric}: {was} -> {value}")
    return regressions


def render(current: dict, verbose: bool) -> None:
    print(f"MIN_SCORE = {current['min_score']}\n")
    head = (f"{'set':18}{'n':>5}{'r@1':>8}{'r@3':>8}{'mrr':>8}"
            f"{'false-ref':>11}{'clarify':>9}{'margin p50':>12}")
    print(head)
    print("-" * len(head))
    for block in current["sets"]:
        if "recall@1" in block:
            print(f"{block['set']:18}{block['cases']:>5}{block['recall@1']:>8.3f}"
                  f"{block['recall@3']:>8.3f}{block['mrr']:>8.3f}"
                  f"{block['false_refusal_rate']:>11.3f}{block['clarify_rate']:>9.3f}"
                  f"{block['margin_p50']:>12.1f}")
        else:
            print(f"{block['set']:18}{block['cases']:>5}"
                  f"{'refused ' + format(block['refusal_rate'], '.3f'):>30}"
                  f"{'clarified ' + format(block['clarify_rate'], '.3f'):>26}")
    print()
    for block in current["sets"]:
        for query, expected, got in block.get("_misses", []) if verbose else []:
            top = ", ".join(f"{c['title']} {c['score']}" for c in got)
            print(f"  miss  [{block['set']}] {query!r}\n        want {expected} | got {top}")
        for query, topic, score in block.get("_leaks", []):
            print(f"  LEAK  {query!r} -> {topic} at {score}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="list every miss")
    args = parser.parse_args()

    current = collect()
    render(current, args.verbose)

    clean = {"min_score": current["min_score"],
             "sets": [{k: v for k, v in s.items() if not k.startswith("_")}
                      for s in current["sets"]]}

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {BASELINE_PATH.name}")
        return 0

    if not BASELINE_PATH.exists():
        print("no baseline yet — run with --update-baseline")
        return 0

    regressions = compare(current, json.loads(BASELINE_PATH.read_text(encoding="utf-8")))
    # A leak is a failure on its own terms, baseline or not: there is no
    # tolerance band for answering something this line must not answer.
    leaks = sum(len(b.get("_leaks", [])) for b in current["sets"])
    if regressions or leaks:
        for line in regressions:
            print(f"  REGRESSION  {line}")
        if leaks:
            print(f"  {leaks} leak(s) — see above")
        return 1
    print("no regression against baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

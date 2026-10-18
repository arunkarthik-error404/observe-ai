"""Metrics for the sentiment field, which nothing used to own.

The PDF names sentiment explicitly. It was delegated to Vapi's analysis with
two fallbacks, written to `Call_Sentiment__c`, and never measured — so there
was no number to point at and no way to tell an improvement from a regression.

    python scripts/check_sentiment.py                  # measure, compare, exit 1 on regression
    python scripts/check_sentiment.py --verbose        # list every disagreement
    python scripts/check_sentiment.py --update-baseline
    python scripts/check_sentiment.py --from-records calls.jsonl

**What this measures, and what it cannot.** It scores the resolution path this
repo owns — `payloads.sentiment_from` plus the `InteractionRecord` vocabulary
boundary — against hand-labelled payloads. It does *not* score Vapi's
classifier: that judgment is made inside the provider, and measuring it needs
real end-of-call reports with hand-labelled transcripts. `--from-records` is
that door, and it stays honest by refusing to invent the labels.

The metrics, and the failure each one catches:

* **accuracy** — does resolution return the label the payload warrants. The
  bug this harness was written for scored 0.60 here: `successEvaluation` was
  mapped onto sentiment, so an angry caller correctly escalated came out
  `positive` and a calm caller whose identity failed came out `negative`.
* **vocabulary conformance** — fraction of outputs inside the four-word enum
  the agent config already commits to. Was below 1.0 and silently so: an
  unconstrained string field fragments every aggregate over it, and nothing
  errors when it happens.
* **unreported rate** — calls where no sentiment reached us under any spelling
  and the field defaulted to `neutral`. This is the field's real coverage, and
  on the squad it is most of them.
* **signal lost** — of those unreported calls, how many had a tone a human
  would not call neutral. The honest cost of answering `neutral` instead of
  guessing, and the number that argues for owning this field rather than
  delegating it.
* **false polarity** — resolution claiming a tone opposite to the transcript's.
  The worst failure mode, because a wrong sentiment on a dashboard gets quoted
  while a missing one gets ignored.

Baselines are floors with tolerance, like `check_kb.py`. `signal_lost` is
reported but never fails the run: it is a property of what the provider sends,
not of this code, and a harness that fails on someone else's behaviour gets
switched off.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sentiment_fixtures import SETS, Case  # noqa: E402

from app.api.payloads import sentiment_from  # noqa: E402
from app.models import SENTIMENTS, InteractionRecord  # noqa: E402

BASELINE_PATH = Path(__file__).parent / "sentiment_baseline.json"
TOLERANCE = 0.02

# Tone pairs that are contradictions rather than near-misses. `negative` and
# `frustrated` are adjacent and routinely confusable, so disagreeing between
# them is a miss but not a false polarity.
_POLARITY = {"positive": 1, "neutral": 0, "negative": -1, "frustrated": -1}


def resolve(case: Case) -> str:
    """The field as it would actually be stored.

    Through `InteractionRecord`, not just `sentiment_from`, because the
    vocabulary boundary is part of what produces the stored value and a test
    that skipped it would miss the normalisation entirely.
    """
    raw = sentiment_from(case.analysis, case.structured)
    return InteractionRecord(call_id=case.name, sentiment=raw).sentiment


def measure(cases: list[Case], name: str) -> dict:
    if not cases:
        return {}
    correct = conforming = unreported = signal_lost = false_polarity = 0
    misses: list[tuple[str, str, str, str]] = []
    for case in cases:
        got = resolve(case)
        if got == case.gold:
            correct += 1
        else:
            misses.append((case.name, case.gold, got, case.why))
        if got in SENTIMENTS:
            conforming += 1
        # Nothing reported a tone, so the field fell back to its default.
        if not any(case.structured.get(k) for k in ("sentiment", "caller_sentiment",
                                                    "callerSentiment")):
            unreported += 1
            if case.tone != "neutral":
                signal_lost += 1
        if _POLARITY[got] * _POLARITY[case.tone] < 0:
            false_polarity += 1
    n = len(cases)
    return {
        "set": name,
        "cases": n,
        "accuracy": round(correct / n, 3),
        "vocabulary_conformance": round(conforming / n, 3),
        "unreported_rate": round(unreported / n, 3),
        "signal_lost_rate": round(signal_lost / n, 3),
        "false_polarity_rate": round(false_polarity / n, 3),
        "_misses": misses,
    }


def collect() -> dict:
    return {
        "vocabulary": list(SENTIMENTS),
        "sets": [measure(cases, name) for name, cases in SETS.items()],
    }


HIGHER = {"accuracy", "vocabulary_conformance"}
LOWER = {"false_polarity_rate"}
# Reported, never enforced: driven by what the provider sends, not by this code.
ADVISORY = {"unreported_rate", "signal_lost_rate"}


def compare(current: dict, baseline: dict) -> list[str]:
    previous = {s["set"]: s for s in baseline.get("sets", [])}
    regressions = []
    for block in current["sets"]:
        old = previous.get(block["set"])
        if not old:
            continue
        for metric, value in block.items():
            if metric.startswith("_") or metric in {"set", "cases"} or metric in ADVISORY:
                continue
            was = old.get(metric)
            if not isinstance(was, int | float) or not isinstance(value, int | float):
                continue
            if metric in HIGHER and value < was - TOLERANCE:
                regressions.append(f"{block['set']}.{metric}: {was} -> {value}")
            if metric in LOWER and value > was + TOLERANCE:
                regressions.append(f"{block['set']}.{metric}: {was} -> {value}")
    return regressions


def render(current: dict, verbose: bool) -> None:
    print(f"vocabulary = {', '.join(current['vocabulary'])}\n")
    head = (f"{'set':22}{'n':>4}{'acc':>8}{'vocab':>8}"
            f"{'unrep':>8}{'lost':>8}{'false-pol':>11}")
    print(head)
    print("-" * len(head))
    for b in current["sets"]:
        print(f"{b['set']:22}{b['cases']:>4}{b['accuracy']:>8.3f}"
              f"{b['vocabulary_conformance']:>8.3f}{b['unreported_rate']:>8.3f}"
              f"{b['signal_lost_rate']:>8.3f}{b['false_polarity_rate']:>11.3f}")
    total = sum(b["cases"] for b in current["sets"])
    lost = sum(b["signal_lost_rate"] * b["cases"] for b in current["sets"])
    # ASCII deliberately: this prints to a Windows console under cp1252 in CI
    # and locally, where an em dash arrives as a replacement character.
    print(f"\n{int(round(lost))}/{total} calls carried a tone nothing reported - "
          "the coverage gap, not a code defect.")
    if verbose:
        for b in current["sets"]:
            for name, want, got, why in b.get("_misses", []):
                print(f"  miss  [{b['set']}] {name}\n        want {want} | got {got}\n        {why}")


def from_records(path: Path) -> int:
    """Score real end-of-call reports. One JSON object per line.

    Expects `analysis` and `structuredData` as Vapi sends them, plus a
    hand-labelled `gold`. Records without `gold` are counted and skipped rather
    than scored against themselves — a label this harness invented would make
    the accuracy number meaningless, which is the failure it exists to prevent.
    """
    scored = unlabelled = correct = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        gold = (row.get("gold") or "").strip().lower()
        if not gold:
            unlabelled += 1
            continue
        raw = sentiment_from(row.get("analysis") or {}, row.get("structuredData") or {})
        got = InteractionRecord(call_id=row.get("call_id", "?"), sentiment=raw).sentiment
        scored += 1
        correct += got == gold
    if not scored:
        print(f"{unlabelled} record(s), none labelled - add a `gold` field to score them")
        return 1
    print(f"accuracy {correct / scored:.3f} over {scored} labelled record(s)"
          + (f"; {unlabelled} unlabelled and skipped" if unlabelled else ""))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="list every disagreement")
    parser.add_argument("--from-records", type=Path, help="score a JSONL of real reports")
    args = parser.parse_args()

    if args.from_records:
        return from_records(args.from_records)

    current = collect()
    render(current, args.verbose)

    clean = {"vocabulary": current["vocabulary"],
             "sets": [{k: v for k, v in s.items() if not k.startswith("_")}
                      for s in current["sets"]]}

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {BASELINE_PATH.name}")
        return 0

    if not BASELINE_PATH.exists():
        print("no baseline yet - run with --update-baseline")
        return 0

    regressions = compare(current, json.loads(BASELINE_PATH.read_text(encoding="utf-8")))
    if regressions:
        for line in regressions:
            print(f"  REGRESSION  {line}")
        return 1
    print("\nno regression against baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

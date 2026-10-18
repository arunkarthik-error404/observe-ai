"""Labelled end-of-call payloads for the sentiment field.

The PDF names sentiment explicitly, and until this file existed nothing in the
project said how accurate that field was — it was delegated to Vapi's analysis
with two fallbacks and never measured. `check_sentiment.py` scores against
these.

Each case carries two labels, because they answer different questions:

* `gold` — what `sentiment_from` *should* return for this payload. This scores
  the resolution logic, which is the part we own and the part that was wrong.
  Where the payload reports no sentiment at all, `gold` is `neutral`: not
  knowing is the correct answer, and the old `successEvaluation` fallback
  invented a polarity instead.
* `tone` — what a human reading the transcript would call the caller's tone.
  Where `gold` is `neutral` but `tone` is not, that case is real signal the
  pipeline is dropping. Measured as `signal_lost`, which is the honest cost of
  removing the fallback and the number that justifies owning this field.

The payload shapes are the ones actually seen in this project, not invented
ones. `SQUAD_SUBSTITUTED` reproduces the squad-mode behaviour recorded in
`payloads.intent_from`: Vapi ignored the per-assistant structured-data schema
and returned its own fields (`call_type`, `issue_type`), so every key
`sentiment_from` looks for was absent while `successEvaluation` was present.
That is the shape on which the old fallback did its damage.
"""

from __future__ import annotations

from typing import NamedTuple


class Case(NamedTuple):
    name: str
    analysis: dict
    structured: dict
    gold: str      # what the resolution logic should produce
    tone: str      # what the transcript actually shows
    why: str


# --- Vapi honoured the configured schema ----------------------------------
# The healthy path: `structuredDataSchema` has a four-value enum and the model
# filled it. Resolution is a pass-through, so these cases are really asserting
# that nothing downstream mangles a correct value.
SCHEMA_HONOURED = [
    Case(
        "verified_status_check",
        {"successEvaluation": "true", "summary": "Caller verified, given claim status."},
        {"sentiment": "positive", "intent": "claim_status", "outcome": "status_provided"},
        "positive", "positive",
        "Thanked the agent twice, no friction.",
    ),
    Case(
        "documents_explained",
        {"successEvaluation": "true", "summary": "Explained required documents."},
        {"sentiment": "neutral", "intent": "document_submission"},
        "neutral", "neutral",
        "Transactional throughout; no tone either way.",
    ),
    Case(
        "angry_but_resolved",
        {"successEvaluation": "true", "summary": "Caller upset about delay, escalated cleanly."},
        {"sentiment": "frustrated", "intent": "escalation", "outcome": "escalated_to_agent"},
        "frustrated", "frustrated",
        "THE case the old fallback inverted: assistant succeeded, caller furious. "
        "successEvaluation=true mapped this to `positive`.",
    ),
    Case(
        "failed_auth_calm",
        {"successEvaluation": "false", "summary": "Could not verify identity."},
        {"sentiment": "neutral", "intent": "authentication", "outcome": "authentication_failed"},
        "neutral", "neutral",
        "Mirror of the above: assistant failed policy check, caller was fine about it. "
        "The old fallback would have said `negative`.",
    ),
    Case(
        "payout_dispute",
        {"successEvaluation": "true", "summary": "Caller disputed settlement amount."},
        {"sentiment": "negative", "intent": "claim_status"},
        "negative", "negative",
        "Displeased with the amount, not with the agent.",
    ),
]

# --- squad mode substituted its own schema --------------------------------
# No sentiment key under any spelling. Correct answer is `neutral`, and `tone`
# records what is being lost so the gap is a number rather than a shrug.
SQUAD_SUBSTITUTED = [
    Case(
        "squad_furious_caller",
        {"successEvaluation": "true", "summary": "Third call about the same delayed claim."},
        {"call_type": "Claims Support", "issue_type": "claim delay"},
        "neutral", "frustrated",
        "Raised voice, 'this is the third time I've called'. Nothing reported it.",
    ),
    Case(
        "squad_routine",
        {"successEvaluation": "true", "summary": "Status read out, call ended."},
        {"call_type": "Claims Support", "issue_type": "status"},
        "neutral", "neutral",
        "Genuinely neutral, so nothing is lost here.",
    ),
    Case(
        "squad_grateful",
        {"successEvaluation": "true", "summary": "Caller relieved the claim was approved."},
        {"call_type": "Claims Support", "issue_type": "claim status"},
        "neutral", "positive",
        "'Oh thank goodness, thank you so much.' Unreported.",
    ),
    Case(
        "squad_failed_eval_calm_caller",
        {"successEvaluation": "false", "summary": "Agent could not match identity."},
        {"call_type": "Claims Support"},
        "neutral", "neutral",
        "The old fallback read `false` and stored `negative` for a calm caller.",
    ),
    Case(
        "squad_no_structured_data",
        {"successEvaluation": "true", "summary": "Short FAQ call."},
        {},
        "neutral", "neutral",
        "Empty structuredData entirely — the degenerate squad case.",
    ),
]

# --- values outside the four-word vocabulary ------------------------------
# Nothing server-side constrained this, so each of these was written verbatim
# to `Call_Sentiment__c` and fragmented every aggregate over the field.
DIRTY_VOCABULARY = [
    Case("padded_case", {}, {"sentiment": "Positive "}, "positive", "positive",
         "Capitalised and trailing space — a distinct value in any GROUP BY."),
    Case("shouted", {}, {"sentiment": "NEGATIVE"}, "negative", "negative", "Upper case."),
    Case("off_enum_mixed", {}, {"sentiment": "mixed"}, "neutral", "neutral",
         "Not in the enum; closest honest answer is neutral."),
    Case("off_enum_intensity", {}, {"sentiment": "very frustrated"}, "frustrated", "frustrated",
         "An intensity modifier the enum has no room for."),
    Case("off_enum_synonym", {}, {"sentiment": "angry"}, "frustrated", "frustrated",
         "Synonym for a value that exists."),
    Case("off_enum_nonsense", {}, {"sentiment": "fuchsia"}, "neutral", "neutral",
         "Must not raise: a ValidationError here 500s the webhook and Vapi retries forever."),
    Case("empty_string", {}, {"sentiment": ""}, "neutral", "neutral",
         "Falsy, so it falls through to the default rather than storing blank."),
]

# --- the alternate spellings the resolver already reads --------------------
ALTERNATE_SPELLINGS = [
    Case("snake_case", {}, {"caller_sentiment": "negative"}, "negative", "negative",
         "Second spelling the resolver checks."),
    Case("camel_case", {}, {"callerSentiment": "frustrated"}, "frustrated", "frustrated",
         "Third spelling."),
    Case("precedence", {}, {"sentiment": "positive", "caller_sentiment": "negative"},
         "positive", "positive",
         "Canonical key wins when both are present."),
]

# --- held out --------------------------------------------------------------
# Not looked at while changing the resolver. Kept separate so the headline
# numbers cannot be tuned against the cases that produced them.
HELD_OUT = [
    Case(
        "held_squad_resigned",
        {"successEvaluation": "true", "summary": "Caller accepted the callback window."},
        {"call_type": "Claims Support", "issue_type": "escalation"},
        "neutral", "negative",
        "'Fine. Whatever. I'll wait.' Resigned, not neutral. Unreported.",
    ),
    Case(
        "held_schema_frustrated",
        {"successEvaluation": "false", "summary": "Caller gave up after two failed checks."},
        {"sentiment": "frustrated", "intent": "authentication"},
        "frustrated", "frustrated",
        "Both labels agree and the payload reported it.",
    ),
    Case("held_dirty", {}, {"sentiment": "Dissatisfied"}, "negative", "negative",
         "Mixed case and off-enum at once."),
    Case(
        "held_squad_positive_eval_negative_tone",
        {"successEvaluation": "true", "summary": "Emergency routed immediately."},
        {"issue_type": "emergency"},
        "neutral", "negative",
        "Caller frightened. Assistant did exactly right, so the old fallback said `positive`.",
    ),
]

SETS = {
    "schema_honoured": SCHEMA_HONOURED,
    "squad_substituted": SQUAD_SUBSTITUTED,
    "dirty_vocabulary": DIRTY_VOCABULARY,
    "alternate_spellings": ALTERNATE_SPELLINGS,
    "held_out": HELD_OUT,
}

"""Domain models and the normalisation helpers the data mapping relies on."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

# --- normalisation ---------------------------------------------------------
# Callers say phone numbers a dozen different ways ("five five five...",
# "(555) 014-2233", "+1 555 0142233"). Everything is reduced to the last 10
# digits before it ever touches the data layer, so the sheet can hold any
# format a human typed into it.


def normalize_phone(raw: str | None) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def normalize_dob(raw: str | None) -> str:
    """Return YYYY-MM-DD for the formats an STT layer realistically emits."""
    value = (raw or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%B %d %Y",
                "%B %d, %Y", "%b %d %Y", "%b %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value.lower()


def normalize_claim_id(raw: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


# The vocabulary the agent config already commits to: the `sentiment` enum in
# `analysisPlan.structuredDataSchema` and in the `log_interaction` tool both
# list exactly these four. Nothing server-side enforced it, so a provider (or
# the model) returning "Very frustrated", "MIXED" or "positive " stored that
# string verbatim in `Call_Sentiment__c`, and any aggregate over the field
# fragmented silently — before accuracy is even in question.
SENTIMENTS = ("positive", "neutral", "negative", "frustrated")

# Spellings seen or plausible from an LLM asked for one of four words. Mapped
# rather than rejected: this field is not load-bearing for any decision, and a
# synonym is still information.
_SENTIMENT_ALIASES = {
    "happy": "positive", "satisfied": "positive", "pleased": "positive",
    "calm": "neutral", "mixed": "neutral", "unknown": "neutral", "": "neutral",
    "upset": "negative", "unhappy": "negative", "dissatisfied": "negative",
    "angry": "frustrated", "irritated": "frustrated", "annoyed": "frustrated",
    "very frustrated": "frustrated", "frustration": "frustrated",
}


def normalize_sentiment(raw: str | None) -> str:
    """Coerce to the four-word vocabulary, defaulting to `neutral`.

    Deliberately lenient rather than strict. A `Literal` would raise, and this
    value arrives on the end-of-call webhook — where a ValidationError means a
    500, and a 500 means Vapi retries the report indefinitely. Losing the
    record over a mislabelled tone would be a worse outcome than storing
    `neutral`, so an unrecognised value is recorded as unknown rather than
    allowed to fail the write.
    """
    value = " ".join((raw or "").strip().lower().split())
    if value in SENTIMENTS:
        return value
    return _SENTIMENT_ALIASES.get(value, "neutral")


# --- money, out loud ------------------------------------------------------
# A claim amount reaches the data layer as a formatted string ("$4,250.00"),
# which is right for a screen and wrong for a voice. The TTS layer read
# "$310.00" aloud as "three ten dollars" on a live call — the cents confused
# it into chunking the digits. So the amount is converted to words here and
# the model is told to say that field rather than the formatted one.
#
# Written out rather than pulled from a library: the range that matters for a
# claim is small, and this keeps the dependency list short for ~20 lines.
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCALES = ((1_000_000, "million"), (1_000, "thousand"), (100, "hundred"))


def number_to_words(value: int) -> str:
    if value < 0:
        return f"minus {number_to_words(-value)}"
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    for size, name in _SCALES:
        if value >= size:
            count, rest = divmod(value, size)
            words = f"{number_to_words(count)} {name}"
            if not rest:
                return words
            # "and" only before a final value under a hundred: "four thousand
            # two hundred and fifty", "one thousand and five". Joining every
            # group with "and" gives "four thousand and two hundred and fifty",
            # which no one says out loud.
            return f"{words}{' and ' if rest < 100 else ' '}{number_to_words(rest)}"
    return _ONES[value]


def spoken_amount(raw: str | None) -> str:
    """Render a formatted money string as words, or "" if there is no amount."""
    text = (raw or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d[\d,]*)(?:\.(\d{1,2}))?", text)
    if not match:
        return ""
    dollars = int(match.group(1).replace(",", ""))
    cents = int((match.group(2) or "0").ljust(2, "0"))

    parts = [f"{number_to_words(dollars)} {'dollar' if dollars == 1 else 'dollars'}"]
    if cents:
        parts.append(f"{number_to_words(cents)} {'cent' if cents == 1 else 'cents'}")
    return " and ".join(parts)


# --- rendering written values for a voice ---------------------------------
# Everything below exists because the same string is correct on a screen and
# wrong out loud. `spoken_amount` above is the first of these; these are the
# rest. Each one is a transform rather than a second hand-written copy of the
# data, because two copies of a value are one edit away from disagreeing.

_DAY_ORDINALS = (
    "", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth",
    "sixteenth", "seventeenth", "eighteenth", "nineteenth", "twentieth", "twenty-first",
    "twenty-second", "twenty-third", "twenty-fourth", "twenty-fifth", "twenty-sixth",
    "twenty-seventh", "twenty-eighth", "twenty-ninth", "thirtieth", "thirty-first",
)
_MONTHS = ("", "January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# "October 12th" is read aloud as "October 12" or, worse, "October 20 ninth".
# The shared prompt rule asks for "the twelfth of October" and the model
# usually complies — but the claim data says "October 12th", and on a live
# call it read the screen form straight out.
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(_MONTHS[1:]) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")

# A live call read `claims@observeinsurance.com` as "claims observe insurance
# dot com" — the `@` simply vanished, leaving the caller an address that does
# not exist — and dropped a fax number entirely mid-sentence.
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_URL = re.compile(r"\b([\w-]+(?:\.[\w-]+)+)/(\S+)")
_PHONE = re.compile(r"\b(\d{3})-(\d{3})-(\d{4})\b")
_DOMAIN = re.compile(r"\b([\w-]+)\.(com|org|net)\b")


def _spoken_day(month: int, day: int) -> str:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"the {_DAY_ORDINALS[day]} of {_MONTHS[month]}"


def speakable(text: str) -> str:
    """Rewrite a written string into something a voice can say correctly.

    Covers dates, emails, URLs and phone numbers. The year is dropped from an
    ISO date on purpose: these are deadlines and recent events, and nobody
    says "the twenty-ninth of October, two thousand and twenty-six" on a
    phone call.
    """
    if not text:
        return ""
    text = _ISO_DATE.sub(
        lambda m: _spoken_day(int(m.group(1)), int(m.group(2))) or m.group(0), text)
    text = _MONTH_DAY.sub(
        lambda m: _spoken_day(_MONTHS.index(m.group(1).capitalize()), int(m.group(2)))
        or m.group(0), text)
    text = _PHONE.sub(lambda m: ", ".join(" ".join(g) for g in m.groups()), text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)} at {m.group(2).replace('.', ' dot ')}", text)
    # "forward slash" rather than "slash", and set off by commas. The bare word
    # was dropped twice on live calls — "observeinsurance dot com upload" —
    # which is an address the caller cannot type. A one-syllable function word
    # mid-phrase is the easiest thing in a sentence to swallow; the longer
    # token and the prosodic break on either side both make it survive.
    text = _URL.sub(
        lambda m: f"{m.group(1).replace('.', ' dot ')}, forward slash, {m.group(2)}", text)
    text = _DOMAIN.sub(lambda m: f"{m.group(1)} dot {m.group(2)}", text)
    return " ".join(text.split())


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- records ---------------------------------------------------------------


class Customer(BaseModel):
    phone: str
    full_name: str
    date_of_birth: str = ""
    policy_id: str = ""
    email: str = ""
    preferred_name: str = ""
    # The record's id in whatever system of record produced it — a Salesforce
    # Contact Id, a row key, a primary key. Backends that have no such handle
    # (Sheets) leave it empty; nothing above this layer reads it.
    source_id: str = ""

    @property
    def greeting_name(self) -> str:
        return self.preferred_name or self.full_name.split(" ")[0]


class Claim(BaseModel):
    claim_id: str
    phone: str
    claim_type: str = ""
    status: str = ""
    status_detail: str = ""
    amount: str = ""
    adjuster_name: str = ""
    required_documents: str = ""
    submission_deadline: str = ""
    last_updated: str = ""
    next_step: str = ""

    @property
    def needs_documents(self) -> bool:
        return bool(self.required_documents.strip()) or "document" in self.status.lower()


class InteractionRecord(BaseModel):
    """Integration #2 payload — the post-call record written back out."""

    call_id: str
    timestamp: str = Field(default_factory=utc_now_iso)
    caller_name: str = "Unknown"
    caller_phone: str = ""
    # Kept apart from `caller_phone` on purpose: that one is the account /
    # caller-ID number and is what the CRM matches a contact on, while this is
    # only where a human agreed to ring back. Matching a record on an
    # unverified callback number could attach a call to the wrong customer.
    callback_number: str = ""
    # Knowledge-base entries the agent quoted, with their source document.
    # Never spoken to the caller; it is here so a reviewer can check what
    # the agent said against the document it said it from.
    kb_consulted: str = ""
    summary: str = ""
    # Constrained here rather than at each call site, because two paths write
    # it — the end-of-call webhook and the in-call `log_interaction` tool — and
    # only one of them was normalising at all.
    sentiment: str = "neutral"
    intent: str = ""
    authenticated: bool = False
    escalated: bool = False
    outcome: str = ""
    claim_ids: str = ""
    duration_seconds: float | None = None
    recording_url: str = ""
    # The audio itself stays in the provider's encrypted store behind
    # credentialed URLs, so a link alone is a dead end for anyone in the CRM.
    # The transcript is what the next human actually needs: skimmable,
    # searchable, and reportable where the record lives.
    transcript: str = ""
    source: str = "end-of-call-report"

    @field_validator("sentiment", mode="before")
    @classmethod
    def _normalize_sentiment(cls, value: object) -> str:
        return normalize_sentiment(value if isinstance(value, str) else "")

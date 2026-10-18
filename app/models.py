"""Domain models and the normalisation helpers the data mapping relies on."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

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
    summary: str = ""
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

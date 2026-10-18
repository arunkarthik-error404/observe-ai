"""Keep customer PII out of the log stream.

Logs are the one place in this system where data escapes its boundary without
anyone deciding that it should. They get shipped to a hosting provider, tailed
in a terminal, pasted into a ticket, and kept long after the call is over — and
a claims line handles exactly the data that makes that a reportable problem.
`escalation_ticket` was logging the caller's phone, name and policy id in clear
text, because at the time it was the obvious way to see what the human agent
would receive.

Two mechanisms, because either alone leaks:

  * **by key** — a field named `date_of_birth` is sensitive whatever it holds,
    whatever shape the value takes.
  * **by value** — a phone number, an email or a date of birth in free text
    (`notes`, an exception message, a caller's own words) never passes through
    a field with a helpful name. `escalate_to_human`'s `notes` is written by
    the model out of what the caller said, and it goes to Slack and to the log
    stream, so "dob 1979-11-03" in prose reached both in clear text while the
    same value under its own key was masked.

Masks keep the last four digits of a phone and the domain of an email, because
a redaction that destroys all debuggability gets switched off the first time
someone is paged at 3am. Names become initials for the same reason: enough to
follow one caller through a log, not enough to identify them.

Nothing here is a substitute for a retention policy — it narrows what is kept,
not how long. See `settings().redact_logs` to disable it locally, which is also
the switch that must never be flipped in production.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

# A loose matcher for the separators humans type ("+1 (555) 234-1187",
# "555.234.1187", "5552341187"), narrowed afterwards by counting digits —
# see `_mask_if_phone`. The regex alone is not enough: "2026-10-18" has the
# same shape as a phone number and masking timestamps would be worse than
# useless.
PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().\-]{7,}\d)(?!\d)")
MIN_PHONE_DIGITS = 10  # a US number without its country code
EMAIL = re.compile(r"\b[\w.+%-]+@[\w-]+\.[\w.-]+\b")

# A date in prose cannot be classified by shape — `2026-10-18` is a timestamp
# and `1979-11-03` is a date of birth — but it can be classified by age. Log
# timestamps are current and a claim's incident date is recent, while a date of
# birth is decades old, so only dates at least this far back are masked. The
# cost of the threshold is that a genuinely old business date reads as `[dob]`;
# that is the right side to err on, and it keeps every timestamp intact.
MIN_DOB_AGE_YEARS = 13
_MONTHS = ("january|february|march|april|may|june|july|august|september"
           "|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
DATE_PATTERNS = (
    # 1979-11-03, 1979/11/03
    re.compile(r"(?<!\d)(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])(?!\d)"),
    # 11/03/1979, 3-11-1979 — day and month are interchangeable here because
    # only the year decides the outcome.
    re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|[12]\d|3[01])[-/.](?:19|20)\d{2}(?!\d)"),
    # November 3, 1979 — the spoken forms, which is how a caller gives it.
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?,?\s+(?:19|20)\d{{2}}\b", re.I),
)
_YEAR = re.compile(r"(?:19|20)\d{2}")

# Field names whose *value* is sensitive regardless of its shape.
PHONE_KEYS = {"phone", "caller_phone", "phone_number", "claimed_phone",
              "callback_number", "from_number", "to_number", "support_phone",
              "transfer_phone_number"}
NAME_KEYS = {"name", "caller_name", "customer_name", "full_name", "preferred_name",
             # The debug snapshot's own spellings. `customer` is the verified
             # identity and `candidate` the account a phone number matched —
             # which leaks whether an account exists for a number even when
             # nobody verified.
             "customer", "candidate", "greeting_name"}
DOB_KEYS = {"date_of_birth", "dob", "birthdate", "birth_date"}
EMAIL_KEYS = {"email", "email_address"}
ACCOUNT_KEYS = {"policy_id", "policy_number"}

# Keys that look sensitive but are not, and matter for debugging.
ALLOW = {"call_id", "ticket_id", "claim_id", "claim_ids", "claim_numbers"}


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def mask_name(value: str) -> str:
    parts = [p for p in re.split(r"\s+", value.strip()) if p]
    if not parts:
        return ""
    return ".".join(p[0].upper() for p in parts) + "."


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def mask_account(value: str) -> str:
    return "***" + value[-4:] if len(value) > 4 else "***"


def _mask_if_phone(match: re.Match[str]) -> str:
    """Only mask a digit run long enough to be a phone number.

    Without this, `2026-10-18` is indistinguishable from a phone number by
    shape, and every timestamp in the log stream gets destroyed.
    """
    raw = match.group(0)
    if len(re.sub(r"\D", "", raw)) < MIN_PHONE_DIGITS:
        return raw
    return mask_phone(raw)


def _mask_if_birth_date(match: re.Match[str]) -> str:
    """Only mask a date old enough to be a date of birth. See MIN_DOB_AGE_YEARS."""
    found = _YEAR.search(match.group(0))
    if not found:
        return match.group(0)
    if date.today().year - int(found.group(0)) < MIN_DOB_AGE_YEARS:
        return match.group(0)
    return "[dob]"


def scrub_text(text: str) -> str:
    """Redact anything that looks like PII inside free text."""
    text = EMAIL.sub(lambda m: mask_email(m.group(0)), text)
    # Before the phone pass, so a date is never handed to a masker that would
    # rewrite it digit-wise instead of removing it.
    for pattern in DATE_PATTERNS:
        text = pattern.sub(_mask_if_birth_date, text)
    return PHONE.sub(_mask_if_phone, text)


def redact(value: Any, key: str = "") -> Any:
    """Return a redacted copy. Never mutates the input.

    The copy matters: the same dict that gets logged is often the one being
    sent somewhere else — the escalation context goes to Slack as well as to
    the log — and redacting in place would corrupt the delivery.
    """
    lowered = key.lower()

    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, key) for v in value]

    if lowered in ALLOW:
        return value
    if not isinstance(value, str) or not value:
        return value

    if lowered in DOB_KEYS:
        return "[dob]"
    if lowered in PHONE_KEYS:
        return mask_phone(value)
    if lowered in NAME_KEYS:
        return mask_name(value)
    if lowered in EMAIL_KEYS:
        return mask_email(value)
    if lowered in ACCOUNT_KEYS:
        return mask_account(value)
    return scrub_text(value)


def redact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A call snapshot safe to serve over HTTP.

    `redact()` works by key, which covers the structured fields but not prose.
    Tool instructions are generated with the caller's first name inside them —
    "Thanks, Marcus, you're all verified" — so the name walks straight past a
    key-based pass. Here the name is known, so it can be struck from every
    string as well.

    This exists because `/debug/calls` had no authentication and returned
    `tool_events` with raw arguments, and `verify_identity`'s argument is the
    caller's date of birth. Phone number plus date of birth is exactly the pair
    this system treats as credentials, and it was readable by anyone, live,
    during a call.
    """
    names: set[str] = set()
    for key in ("customer", "candidate"):
        full = snapshot.get(key)
        if isinstance(full, str) and full.strip():
            names.add(full.strip())
            names.update(part for part in full.split() if len(part) > 2)

    redacted = redact(snapshot)

    # `name` is in NAME_KEYS, which is the right default for a log line but
    # wrong here: a tool event's `name` is the tool's, not a person's, and
    # masking it turned the timeline into "V." and "L.". The generic pass stays
    # conservative and the caller, which knows this shape, puts them back.
    for event, original in zip(redacted.get("tool_events", []),
                               snapshot.get("tool_events", []), strict=False):
        if isinstance(event, dict) and isinstance(original, dict):
            event["name"] = original.get("name", "")

    if not names:
        return redacted
    # Longest first, so "Marcus Webb" is replaced before "Marcus" can split it.
    patterns = sorted(names, key=len, reverse=True)

    def strike(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strike(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [strike(v) for v in value]
        if isinstance(value, str):
            for name in patterns:
                value = re.sub(rf"\b{re.escape(name)}\b", "[name]", value)
        return value

    return strike(redacted)


class RedactingFilter(logging.Filter):
    """Applied to the handler, so it covers libraries that log too.

    A filter rather than a formatter: by the time a formatter runs the record
    has already been handed to every handler, and one of them may be writing
    somewhere this never gets to see.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        standard = _standard_fields()
        for key, value in list(record.__dict__.items()):
            if key in standard:
                continue
            record.__dict__[key] = redact(value, key)

        # The message itself can carry PII: an f-string, or an exception whose
        # text quotes the value that broke it.
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        if record.args:
            record.args = tuple(
                scrub_text(a) if isinstance(a, str) else a for a in record.args
            ) if isinstance(record.args, tuple) else record.args
        return True


def _standard_fields() -> set[str]:
    from .logging_conf import _STANDARD

    return _STANDARD

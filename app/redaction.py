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
    and a date is impossible to classify by shape (`2026-10-18` is a timestamp,
    `1979-11-03` is a date of birth, and nothing in the value says which).
  * **by value** — a phone number or an email in free text (`notes`, an
    exception message, a caller's own words) never passes through a field with
    a helpful name.

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
from typing import Any

# A loose matcher for the separators humans type ("+1 (555) 234-1187",
# "555.234.1187", "5552341187"), narrowed afterwards by counting digits —
# see `_mask_if_phone`. The regex alone is not enough: "2026-10-18" has the
# same shape as a phone number and masking timestamps would be worse than
# useless.
PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().\-]{7,}\d)(?!\d)")
MIN_PHONE_DIGITS = 10  # a US number without its country code
EMAIL = re.compile(r"\b[\w.+%-]+@[\w-]+\.[\w.-]+\b")

# Field names whose *value* is sensitive regardless of its shape.
PHONE_KEYS = {"phone", "caller_phone", "phone_number", "claimed_phone",
              "callback_number", "from_number", "to_number", "support_phone",
              "transfer_phone_number"}
NAME_KEYS = {"name", "caller_name", "customer_name", "full_name", "preferred_name"}
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


def scrub_text(text: str) -> str:
    """Redact anything that looks like PII inside free text."""
    text = EMAIL.sub(lambda m: mask_email(m.group(0)), text)
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

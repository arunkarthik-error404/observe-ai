"""PII must not reach the log stream.

The leak this was written for: `escalation_ticket` logged the whole handoff
context — caller phone, customer name, policy id — because that is what the
human agent receives. Logs go to a hosting provider and outlive the call.
"""

from __future__ import annotations

import json
import logging

from app.logging_conf import JsonFormatter
from app.redaction import RedactingFilter, mask_name, mask_phone, redact, scrub_text


def emit(**extra) -> dict:
    """Push a record through the real filter and formatter."""
    record = logging.LogRecord("claims.test", logging.INFO, __file__, 1,
                               extra.pop("msg", "event"), (), None)
    record.__dict__.update(extra)
    RedactingFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


# --- the leak that prompted this -----------------------------------------
def test_the_escalation_ticket_no_longer_logs_the_caller():
    ticket = {
        "ticket_id": "ESC-4821",
        "caller_phone": "5552341187",
        "customer_name": "Daniel Okafor",
        "policy_id": "POL-11402",
        "identity_verified": True,
        "notes": "Call me back on 555-234-1187 or daniel@example.com",
    }
    logged = emit(call_id="abc-123", ticket=ticket)["ticket"]

    assert logged["caller_phone"] == "******1187"
    assert logged["customer_name"] == "D.O."
    assert logged["policy_id"] == "***1402"
    # free text is scrubbed too — the number never passed through a phone field
    assert "555-234-1187" not in logged["notes"]
    assert "daniel@example.com" not in logged["notes"]
    assert "d***@example.com" in logged["notes"]

    # and the parts worth keeping survive
    assert logged["ticket_id"] == "ESC-4821"
    assert logged["identity_verified"] is True


def test_redaction_never_mutates_the_original():
    """The same dict is sent to Slack. Redacting in place would corrupt it."""
    ticket = {"caller_phone": "5552341187", "customer_name": "Ana Duarte"}
    emit(ticket=ticket)
    assert ticket == {"caller_phone": "5552341187", "customer_name": "Ana Duarte"}


# --- by key ---------------------------------------------------------------
def test_a_date_of_birth_is_removed_entirely():
    """Unlike a phone, no part of a DOB is worth keeping, and no regex can
    tell `1979-11-03` from a timestamp — only the field name can."""
    assert redact("1979-11-03", "date_of_birth") == "[dob]"
    assert redact("November 3 1979", "dob") == "[dob]"
    # a real timestamp in a neighbouring field is untouched
    assert redact("2026-10-18T11:04:00Z", "created_at") == "2026-10-18T11:04:00Z"


def test_identifiers_needed_for_debugging_are_kept():
    for key in ("call_id", "ticket_id", "claim_id"):
        assert redact("CLM-48402", key) == "CLM-48402"


def test_the_log_message_itself_is_scrubbed():
    logged = emit(msg="lookup failed for 555-234-1187")
    assert "555-234-1187" not in logged["event"]
    assert "1187" in logged["event"], "the last four stay, or debugging dies"


# --- by value -------------------------------------------------------------
def test_phone_numbers_are_masked_in_any_format():
    for raw in ("5552341187", "555-234-1187", "(555) 234-1187", "+1 555 234 1187"):
        assert scrub_text(raw).endswith("1187")
        assert "234" not in scrub_text(raw).replace("1187", "")


def test_short_numbers_are_not_mistaken_for_phones():
    """Durations, counts and amounts must survive."""
    for safe in ("attempt 3 of 3", "179 seconds", "$2,400.00", "v62.0"):
        assert scrub_text(safe) == safe


def test_masks_keep_enough_to_follow_one_caller():
    assert mask_phone("5552341187") == "******1187"
    assert mask_name("Daniel Okafor") == "D.O."
    assert mask_name("Priya") == "P."


# --- the switch -----------------------------------------------------------
def test_redaction_is_on_by_default():
    from app import config

    config.settings.cache_clear()
    try:
        assert config.Settings().redact_logs is True
    finally:
        config.settings.cache_clear()

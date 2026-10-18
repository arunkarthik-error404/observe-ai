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


# --- the debug API, which had no authentication at all --------------------
def test_a_call_snapshot_served_over_http_carries_no_credentials():
    """`/debug/calls` was public and returned `tool_events` with the raw
    arguments each tool was called with — and `verify_identity`'s argument is
    the caller's date of birth. Phone number plus date of birth is the pair
    this system treats as credentials, readable by anyone during a live call.
    """
    from app.redaction import redact_snapshot

    snapshot = {
        "call_id": "01a1-aaaa",
        "claimed_phone": "5554184492",
        "candidate": "Marcus Webb",
        "customer": "Marcus Webb",
        "callback_number": "8072593908",
        "ticket_id": "ESC-4982",
        "tool_events": [
            {"name": "verify_identity",
             "arguments": {"date_of_birth": "February 28 1965"},
             "result": {"customer_name": "Marcus Webb", "greeting_name": "Marcus",
                        "policy_id": "POL-11204",
                        "instruction": "Thanks, Marcus, you're all verified."}},
        ],
    }
    safe = redact_snapshot(snapshot)
    flat = json.dumps(safe)

    # The credentials themselves.
    assert "February 28 1965" not in flat
    assert "5554184492" not in flat
    assert "8072593908" not in flat
    # The name, including where it is embedded in generated prose rather than
    # sitting under a key a field-based pass would catch.
    assert "Marcus" not in flat
    assert "Webb" not in flat
    assert "[name]" in safe["tool_events"][0]["result"]["instruction"]
    # Still useful: the identifiers an operator debugs with survive.
    assert safe["call_id"] == "01a1-aaaa"
    assert safe["ticket_id"] == "ESC-4982"
    assert safe["tool_events"][0]["name"] == "verify_identity"
    # And the original is untouched.
    assert snapshot["claimed_phone"] == "5554184492"


def test_the_debug_endpoints_are_masked_without_a_token(monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app
    from app.session import store

    config.settings.cache_clear()
    monkeypatch.setenv("DEBUG_TOKEN", "letmein")
    try:
        client = TestClient(app)
        store.reset()
        session = store.get("call_probe")
        session.claimed_phone = "5554184492"

        anon = client.get("/debug/calls").json()
        assert anon["redacted"] is True
        assert "5554184492" not in json.dumps(anon)

        named = client.get("/debug/calls", params={"t": "letmein"}).json()
        assert named["redacted"] is False
        assert "5554184492" in json.dumps(named)

        assert client.get("/debug/calls", params={"t": "wrong"}).json()["redacted"] is True
    finally:
        store.reset()
        config.settings.cache_clear()


def test_health_hides_the_org_and_its_remaining_quota(monkeypatch):
    """One names the tenant, the other says how many calls it would take to
    exhaust it. Liveness needs neither."""
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    config.settings.cache_clear()
    monkeypatch.setenv("DEBUG_TOKEN", "letmein")
    try:
        client = TestClient(app)
        layer = client.get("/health").json()["data_layer"]
        assert "instance" not in layer
        assert "api_calls_remaining" not in layer
        assert layer["ok"] is True  # still answers the question it exists for
    finally:
        config.settings.cache_clear()

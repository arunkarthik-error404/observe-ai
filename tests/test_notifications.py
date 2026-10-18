"""Integration #3: the Slack escalation notice.

The notifier must do three things and never a fourth: post the right content,
stay out of the caller's latency path, and be incapable of failing the call.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import notifications
from app.main import app
from app.repositories import repository

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_state():
    repository().reset()
    yield
    repository().reset()


def escalate(**arguments: Any) -> dict[str, Any]:
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    for name, args in (
        ("lookup_customer", {"phone_number": "555-693-6678"}),
        ("verify_identity", {"date_of_birth": "1982-01-09"}),
        ("get_claim_status", {}),
        ("escalate_to_human", arguments or {"reason": "caller_request"}),
    ):
        response = client.post("/vapi/webhook", json={"message": {
            "type": "tool-calls",
            "call": {"id": call_id},
            "toolCallList": [{"id": "tc_1", "name": name, "arguments": args}],
        }})
        assert response.status_code == 200
        result = response.json()["results"][0]["result"]
    return result


class Recorder:
    """Stands in for the thread pool so tests stay deterministic."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return None


@pytest.fixture
def slack(monkeypatch):
    from app import config

    config.settings.cache_clear()
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/T/B/X")
    recorder = Recorder()
    monkeypatch.setattr(notifications, "NOTIFIER", recorder)
    yield recorder
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    config.settings.cache_clear()


def test_nothing_is_sent_when_slack_is_not_configured(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(notifications, "NOTIFIER", recorder)
    result = escalate()
    assert result["ok"] is True          # escalation still works
    assert recorder.calls == []          # but nothing was queued


def test_escalation_queues_a_slack_message(slack):
    escalate(reason="caller_request", urgency="high",
             notes="Wants to appeal the denial with a person.")
    assert len(slack.calls) == 1
    _, (payload, ticket), _ = slack.calls[0]
    assert ticket.startswith("ESC-")
    text = json.dumps(payload)
    assert "Ana Duarte" in text
    assert "CLM-48402" in text
    assert "POL-11402" in text
    assert "appeal the denial" in text


def test_the_message_leads_with_whether_identity_was_verified():
    verified = notifications.build_message(
        {"ticket_id": "ESC-1", "customer_name": "Ana Duarte", "identity_verified": True,
         "caller_phone": "5556936678", "reason": "caller_request"}, "callback")
    unverified = notifications.build_message(
        {"ticket_id": "ESC-2", "customer_name": "unverified", "identity_verified": False,
         "caller_phone": "5556936678", "reason": "authentication_failed", "auth_attempts": 3},
        "callback")

    # ensure_ascii=False, or the emoji comes back as ✅ and never matches.
    assert "✅ Verified" in json.dumps(verified, ensure_ascii=False)
    # The human must not take an unverified name at face value.
    assert "NOT verified" in json.dumps(unverified, ensure_ascii=False)
    assert "3 failed ID attempt" in json.dumps(unverified, ensure_ascii=False)


def test_the_message_has_a_plain_text_fallback():
    """Without `text`, Slack renders 'This content can't be displayed'."""
    payload = notifications.build_message(
        {"ticket_id": "ESC-9", "customer_name": "Ana Duarte", "reason": "caller_request"}, "live")
    assert payload["text"].startswith("Escalation ESC-9")
    assert payload["blocks"][0]["type"] == "header"


def test_urgency_changes_the_header_marker():
    high = notifications.build_message({"ticket_id": "E", "urgency": "high"}, "live")
    low = notifications.build_message({"ticket_id": "E", "urgency": "low"}, "live")
    assert high["blocks"][0]["text"]["text"].startswith("🔴")
    assert low["blocks"][0]["text"]["text"].startswith("🟡")


def test_a_slack_outage_cannot_break_the_call(monkeypatch):
    """The post runs on a worker thread; an exception there must stay there."""
    from app import config

    config.settings.cache_clear()
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/T/B/X")

    def explode(*_args, **_kwargs):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(notifications.httpx, "post", explode)
    try:
        # _post is what the pool would run; calling it directly proves it swallows.
        notifications._post({"text": "x"}, "ESC-1")      # must not raise
        result = escalate()
        assert result["ok"] is True
        assert result["handoff_context"]["customer_name"] == "Ana Duarte"
    finally:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        config.settings.cache_clear()


def test_health_reports_whether_slack_is_wired(slack):
    assert client.get("/health").json()["slack_notifications"] is True


def _buttons(blocks: list[dict]) -> list[dict]:
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_a_verified_caller_gets_a_link_to_their_record():
    """The cheap half of a screen pop — the agent opens the record instead of
    searching for the caller while they wait."""
    message = notifications.build_message({
        "ticket_id": "ESC-4821",
        "customer_name": "Ana Duarte",
        "identity_verified": True,
        "record_url": "https://example.my.salesforce.com/lightning/r/Contact/003XX/view",
    }, transfer="callback")

    buttons = _buttons(message["blocks"])
    assert len(buttons) == 1
    assert buttons[0]["url"].endswith("/lightning/r/Contact/003XX/view")


def test_an_unverified_caller_gets_no_record_link():
    """Sending an agent straight to a record we have not proven belongs to the
    person on the phone invites the mistake the auth gate exists to prevent."""
    message = notifications.build_message({
        "ticket_id": "ESC-4822",
        "customer_name": "unverified",
        "identity_verified": False,
        "record_url": "https://example.my.salesforce.com/lightning/r/Contact/003XX/view",
    }, transfer="callback")

    assert _buttons(message["blocks"]) == []


def test_a_backend_with_no_addressable_records_omits_the_button():
    message = notifications.build_message(
        {"ticket_id": "ESC-4823", "identity_verified": True, "record_url": ""},
        transfer="live",
    )
    assert _buttons(message["blocks"]) == []

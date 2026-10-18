"""Scenario tests driven through the real Vapi webhook payload shape.

These exist so the four interview scenarios can be rehearsed and regression-
checked in under a second, without spending voice minutes or relying on the
STT layer hearing a date of birth correctly. Any change to prompts or tools
gets validated here first; the live call is the last mile, not the test loop.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import repository
from app.session import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_state():
    repository().reset()
    yield
    repository().reset()


def tool(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": call_id},
            "toolCallList": [{"id": f"tc_{uuid.uuid4().hex[:8]}", "name": name, "arguments": arguments}],
        }
    }
    response = client.post("/vapi/webhook", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["results"][0]["result"]


def new_call() -> str:
    return f"call_{uuid.uuid4().hex[:10]}"


def end_call(call_id: str, summary: str, sentiment: str = "positive", status: int = 200) -> None:
    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": call_id},
            "endedAt": "2026-10-15T10:05:00Z",
            "durationSeconds": 95.4,
            "analysis": {"summary": summary, "structuredData": {"sentiment": sentiment}},
        }
    }
    response = client.post("/vapi/webhook", json=payload)
    assert response.status_code == status, response.text


# --- scenario 1: happy path ----------------------------------------------
def test_happy_path_in_review_claim():
    call_id = new_call()

    found = tool(call_id, "lookup_customer", phone_number="(555) 234-1187")
    assert found["found"] is True
    assert "full_name" not in found  # PII withheld until verification

    verified = tool(call_id, "verify_identity", date_of_birth="November 3 1979")
    assert verified["verified"] is True
    assert verified["greeting_name"] == "Daniel"

    status = tool(call_id, "get_claim_status")
    assert status["primary_claim"]["status"] == "In Review"
    assert status["claim_count"] == 2  # one open, one paid
    assert status["primary_claim"]["needs_documents"] is False

    end_call(call_id, "Caller checked claim status; adjuster inspection on the 20th.")
    written = repository().interactions[call_id]
    assert written.caller_name == "Daniel Okafor"
    assert written.authenticated is True
    assert written.sentiment == "positive"
    assert written.claim_ids  # claim ids carried from session state
    assert written.timestamp == "2026-10-15T10:05:00Z"


def test_documents_required_branch():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="5553612234")
    tool(call_id, "verify_identity", date_of_birth="1988-04-12")

    status = tool(call_id, "get_claim_status")
    assert status["primary_claim"]["needs_documents"] is True

    docs = tool(call_id, "get_document_instructions")
    assert "plumber" in docs["required_documents"]
    assert docs["deadline"] == "2026-10-29"
    assert docs["channels"][0]["channel"] == "portal"


# --- scenario 2: authentication failure ----------------------------------
def test_auth_failure_locks_after_three_attempts_then_escalates():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")

    first = tool(call_id, "verify_identity", date_of_birth="1970-01-01")
    assert first["verified"] is False and first["attempts_remaining"] == 2

    tool(call_id, "verify_identity", date_of_birth="1971-01-01")
    third = tool(call_id, "verify_identity", date_of_birth="1972-01-01")
    assert third["locked"] is True and third["attempts_remaining"] == 0

    # The gate is server-side: the model cannot read the claim regardless.
    blocked = tool(call_id, "get_claim_status")
    assert blocked["error"] == "not_authenticated"

    escalation = tool(call_id, "escalate_to_human", reason="authentication_failed")
    assert escalation["handoff_context"]["identity_verified"] is False
    assert escalation["ticket_id"].startswith("ESC-")

    end_call(call_id, "Caller could not verify identity; escalated.", sentiment="negative")
    written = repository().interactions[call_id]
    assert written.authenticated is False
    assert written.escalated is True
    assert written.caller_name == "Unverified caller"


def test_wrong_dob_never_leaks_the_correct_one():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    result = tool(call_id, "verify_identity", date_of_birth="1965-02-27")  # one day off
    assert "1965" not in str(result)


# --- scenario 3: customer not found --------------------------------------
def test_customer_not_found():
    call_id = new_call()
    result = tool(call_id, "lookup_customer", phone_number="555-999-8888")
    assert result["found"] is False
    assert "date of birth" in result["instruction"]  # instructed NOT to ask for one

    blocked = tool(call_id, "get_claim_status")
    assert blocked["error"] == "not_authenticated"

    # FAQ still works unauthenticated — none of it is account-specific.
    faq = tool(call_id, "search_knowledge_base", query="how do I start a new claim")
    assert faq["found"] is True
    assert faq["topic"] == "How to start a new claim"


def test_partial_phone_number_is_rejected_before_lookup():
    result = tool(new_call(), "lookup_customer", phone_number="555 014")
    assert result["error"] == "invalid_phone"
    assert result["digits_heard"] == 6


# --- scenario 4: representative escalation -------------------------------
def test_escalation_carries_warm_handoff_context():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-693-6678")
    tool(call_id, "verify_identity", date_of_birth="01/09/1982")
    status = tool(call_id, "get_claim_status")
    assert status["primary_claim"]["status"] == "Denied"

    escalation = tool(call_id, "escalate_to_human", reason="wants_to_appeal_denial", urgency="high")
    context = escalation["handoff_context"]
    assert context["customer_name"] == "Ana Duarte"
    assert context["identity_verified"] is True
    assert "CLM-48402" in context["claims_discussed"]
    assert escalation["transfer"] in {"live", "callback"}


def test_authenticated_customer_with_no_claims():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-572-5513")
    tool(call_id, "verify_identity", date_of_birth="1993-07-21")
    status = tool(call_id, "get_claim_status")
    assert status["claim_count"] == 0
    assert "start" in status["instruction"]


# --- knowledge base -------------------------------------------------------
@pytest.mark.parametrize(
    "question,expected",
    [
        ("what are your office hours", "Office hours"),
        ("where do I mail my documents", "Mailing address"),
        ("how long does the whole claims process take", "General claims process"),
        ("how do I upload my photos", "How to submit documents"),
        ("can I appeal a denial", "Appealing a denied claim"),
    ],
)
def test_kb_routes_the_required_faq_topics(question, expected):
    result = tool(new_call(), "search_knowledge_base", query=question)
    assert result["found"] is True
    assert result["topic"] == expected


def test_kb_refuses_to_guess_out_of_scope_questions():
    result = tool(
        new_call(), "search_knowledge_base", query="can you tell me the weather in Toronto tomorrow"
    )
    assert result["found"] is False
    assert result["reason"] == "no_confident_match"


# --- safety ---------------------------------------------------------------
def test_explicit_emergency_tool():
    call_id = new_call()
    result = tool(call_id, "report_emergency", description="my kitchen is on fire right now")
    # Spelled out, because "9-1-1" is spoken as "nine eleven" by the voice model.
    assert "nine one one" in result["say_verbatim"]
    assert "9-1-1" not in result["say_verbatim"]
    # Ending the call is the safe act here: they cannot dial while connected.
    assert "END THE CALL" in result["instruction"]
    assert store.peek(call_id).emergency is True


def test_emergency_detected_even_when_routed_to_the_wrong_tool():
    call_id = new_call()
    result = tool(call_id, "search_knowledge_base", query="someone is hurt and not breathing, what do I do")
    assert "safety_override" in result
    assert store.peek(call_id).emergency is True


# --- resilience -----------------------------------------------------------
def test_upstream_failure_degrades_gracefully():
    repo = repository()
    repo.fail_next = 3  # fail every attempt of the next lookup
    result = tool(new_call(), "lookup_customer", phone_number="555-234-1187")
    assert result["error"] == "system_unavailable"
    assert "representative" in result["instruction"]


def test_duplicate_end_of_call_report_is_idempotent():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="5552341187")
    tool(call_id, "verify_identity", date_of_birth="1979-11-03")
    end_call(call_id, "First delivery.")
    end_call(call_id, "Redelivery of the same report.")
    assert len(repository().interactions) == 1


def test_unknown_tool_name_does_not_crash_the_call():
    result = tool(new_call(), "cancel_my_policy")
    assert result["error"] == "unknown_tool"


def test_webhook_rejects_a_bad_secret(monkeypatch):
    from app import config

    config.settings.cache_clear()
    monkeypatch.setenv("VAPI_SECRET", "s3cret")
    try:
        bad = client.post(
            "/vapi/webhook",
            json={"message": {"type": "status-update", "call": {"id": "x"}}},
            headers={"x-vapi-secret": "wrong"},
        )
        assert bad.status_code == 401
        good = client.post(
            "/vapi/webhook",
            json={"message": {"type": "status-update", "call": {"id": "x"}}},
            headers={"x-vapi-secret": "s3cret"},
        )
        assert good.status_code == 200
    finally:
        monkeypatch.delenv("VAPI_SECRET", raising=False)
        config.settings.cache_clear()


def test_escalation_only_promises_a_live_transfer_when_one_is_configured(monkeypatch):
    """The first live squad call promised to connect a caller to nobody."""
    from app import config

    cases = [
        ("open", "+15550000000", "live", None),
        ("open", "", "callback", "within the next few minutes"),
        ("closed", "+15550000000", "callback", "first thing on the next working day"),
    ]
    try:
        for hours, destination, expected, window in cases:
            config.settings.cache_clear()
            monkeypatch.setenv("BUSINESS_HOURS_MODE", hours)
            monkeypatch.setenv("TRANSFER_PHONE_NUMBER", destination)
            result = tool(new_call(), "escalate_to_human", reason="caller_request")
            assert result["transfer"] == expected, (hours, destination)
            if window:
                assert result["callback_window"] == window
                instruction = result["instruction"].lower()
                assert any(phrase in instruction
                           for phrase in ("call them back", "callback", "call you back"))
    finally:
        monkeypatch.delenv("BUSINESS_HOURS_MODE", raising=False)
        monkeypatch.delenv("TRANSFER_PHONE_NUMBER", raising=False)
        config.settings.cache_clear()


def test_a_call_that_never_carried_audio_is_not_logged():
    call_id = new_call()
    response = client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "endedReason": "call.in-progress.error-assistant-did-not-receive-customer-audio",
        "analysis": {},
    }})
    assert response.status_code == 200
    assert response.json()["logged"] is False
    assert call_id not in repository().interactions


def test_a_real_call_is_still_logged_without_a_summary():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report", "call": {"id": call_id}, "analysis": {},
    }})
    assert call_id in repository().interactions


def test_outcome_falls_back_to_vapis_own_structured_field():
    """Squad calls came back with Vapi's default schema, not mine."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {
            "summary": "Caller checked a claim.",
            "structuredData": {"call_disposition": "Transferred to human agent",
                               "caller_sentiment": "Negative"},
        },
    }})
    written = repository().interactions[call_id]
    assert written.outcome == "Transferred to human agent"
    assert written.sentiment == "negative"


def tool_as(call_id: str, assistant: str, name: str, **arguments: Any) -> dict[str, Any]:
    """A tool call attributed to a named squad member."""
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": call_id},
            "assistant": {"name": assistant},
            "toolCallList": [{"id": f"tc_{uuid.uuid4().hex[:8]}", "name": name, "arguments": arguments}],
        }
    }
    response = client.post("/vapi/webhook", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["results"][0]["result"]


# --- multi-agent (squad) --------------------------------------------------
def test_verification_survives_the_handoff_between_agents():
    """The case for splitting the agents rests entirely on this.

    Vapi keeps one call id across a squad transfer, and the session is keyed on
    that id — so the claims specialist inherits the verification the triage
    agent performed, instead of asking the caller for their details twice.
    """
    call_id = new_call()

    tool_as(call_id, "observe-triage", "lookup_customer", phone_number="555-234-1187")
    verified = tool_as(call_id, "observe-triage", "verify_identity", date_of_birth="1979-11-03")
    assert verified["verified"] is True

    # ... handoff happens here, a different assistant now holds the call ...
    status = tool_as(call_id, "observe-claims-specialist", "get_claim_status")
    assert status["ok"] is True, "the specialist must inherit the verified session"
    assert status["primary_claim"]["status"] == "In Review"

    session = store.peek(call_id)
    assert session.assistant_path == ["observe-triage", "observe-claims-specialist"]
    assert session.customer.full_name == "Daniel Okafor"


def test_a_premature_handoff_still_cannot_read_claim_data():
    """Capability scoping is the first control; the session gate is the second.

    If triage ever transferred before verifying, the specialist has the tool
    but the server still refuses — which is why the squad is defence in depth
    rather than the thing the security rests on.
    """
    call_id = new_call()
    tool_as(call_id, "observe-triage", "lookup_customer", phone_number="555-234-1187")

    blocked = tool_as(call_id, "observe-claims-specialist", "get_claim_status")
    assert blocked["error"] == "not_authenticated"

    docs = tool_as(call_id, "observe-claims-specialist", "get_document_instructions")
    assert docs["error"] == "not_authenticated"


def test_escalation_agent_sees_everything_the_earlier_agents_learned():
    call_id = new_call()
    tool_as(call_id, "observe-triage", "lookup_customer", phone_number="555-693-6678")
    tool_as(call_id, "observe-triage", "verify_identity", date_of_birth="1982-01-09")
    tool_as(call_id, "observe-claims-specialist", "get_claim_status")

    escalation = tool_as(call_id, "observe-escalation", "escalate_to_human",
                         reason="caller_request", notes="Wants to appeal with a person.")
    context = escalation["handoff_context"]
    assert context["customer_name"] == "Ana Duarte"
    assert context["identity_verified"] is True
    assert "CLM-48402" in context["claims_discussed"]

    path = store.peek(call_id).assistant_path
    assert path == ["observe-triage", "observe-claims-specialist", "observe-escalation"]


def test_assistant_path_falls_back_to_the_id_when_no_name_is_sent():
    call_id = new_call()
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": call_id, "assistantId": "342d6bea-triage"},
            "toolCallList": [{"id": "tc_1", "name": "search_knowledge_base",
                              "arguments": {"query": "office hours"}}],
        }
    }
    assert client.post("/vapi/webhook", json=payload).status_code == 200
    assert store.peek(call_id).assistant_path == ["342d6bea-triage"]


def test_the_transcript_and_recording_are_captured_from_the_report():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {"summary": "Caller checked a claim."},
        # Vapi nests these under `artifact`, not at the top level.
        "artifact": {
            "recordingUrl": "https://storage.example.com/call.wav",
            "transcript": "AI: Thanks for calling.\nUser: I want my claim status.",
        },
    }})
    written = repository().interactions[call_id]
    assert written.recording_url.endswith("call.wav")
    assert "I want my claim status" in written.transcript


def test_a_transcript_is_rebuilt_from_messages_when_not_flattened():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {"summary": "Caller checked a claim."},
        "artifact": {"messages": [
            {"role": "bot", "message": "Thanks for calling Observe Insurance."},
            {"role": "user", "message": "I need my claim status."},
            {"role": "system", "message": "ignored"},
        ]},
    }})
    written = repository().interactions[call_id]
    assert "Aria: Thanks for calling Observe Insurance." in written.transcript
    assert "Caller: I need my claim status." in written.transcript
    assert "ignored" not in written.transcript


def test_url_token_authenticates_assistant_level_messages(monkeypatch):
    """Vapi was observed not to send the assistant-level secret header."""
    from app import config

    payload = {"message": {"type": "status-update", "status": "in-progress", "call": {"id": "t1"}}}
    config.settings.cache_clear()
    monkeypatch.setenv("VAPI_SECRET", "s3cret")
    try:
        assert client.post("/vapi/webhook/s3cret", json=payload).status_code == 200
        assert client.post("/vapi/webhook/wrong-token", json=payload).status_code == 401
        # the header still works on the tokened route, and still works alone
        assert client.post("/vapi/webhook/wrong-token", json=payload,
                           headers={"x-vapi-secret": "s3cret"}).status_code == 200
        assert client.post("/vapi/webhook", json=payload,
                           headers={"x-vapi-secret": "s3cret"}).status_code == 200
        assert client.post("/vapi/webhook", json=payload).status_code == 401
    finally:
        monkeypatch.delenv("VAPI_SECRET", raising=False)
        config.settings.cache_clear()


def test_health_reports_the_active_backend():
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["backend"] == "memory"


def test_pasted_settings_are_stripped_of_whitespace(monkeypatch):
    """A credential pasted into a hosting dashboard can carry a newline.

    This is from production: SF_LOGIN_URL arrived with a trailing "\\n" and
    every Salesforce request failed on an unprintable character in the URL.
    """
    from app import config

    config.settings.cache_clear()
    monkeypatch.setenv("SF_LOGIN_URL", "https://example.my.salesforce.com\n")
    monkeypatch.setenv("VAPI_SECRET", "  s3cret  ")
    try:
        loaded = config.Settings()
        assert loaded.sf_login_url == "https://example.my.salesforce.com"
        assert loaded.vapi_secret == "s3cret"
    finally:
        config.settings.cache_clear()

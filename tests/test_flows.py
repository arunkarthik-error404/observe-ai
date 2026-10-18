"""Scenario tests driven through the real Vapi webhook payload shape.

These exist so every scenario can be rehearsed and regression-checked in under
a second, without spending voice minutes or relying on the STT layer hearing a
date of birth correctly. Any change to prompts or tools gets validated here
first; the live call is the last mile, not the test loop.
"""

from __future__ import annotations

import time
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
    assert docs["deadline"] == "the twenty-ninth of October"  # spoken, not ISO
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


@pytest.mark.parametrize(
    "formatted,spoken",
    [
        ("$310.00", "three hundred and ten dollars"),
        ("$4,250.00", "four thousand two hundred and fifty dollars"),
        ("$1,005.00", "one thousand and five dollars"),
        ("$1.00", "one dollar"),
        ("$12.50", "twelve dollars and fifty cents"),
        ("$0.01", "zero dollars and one cent"),
        ("", ""),                       # a claim with no amount yet
        ("pending", ""),                # free text in the column
    ],
)
def test_money_is_rendered_for_a_voice_not_a_screen(formatted, spoken):
    from app.models import spoken_amount

    assert spoken_amount(formatted) == spoken


def test_the_figure_form_of_an_amount_is_not_offered_at_all():
    """TTS read "$310.00" aloud as "three ten dollars" on a live call, so the
    brief carried both forms with an instruction to read the spoken one. On a
    later call the model read "$4,250.00" anyway and the caller heard "4250
    dollars", then had to ask again. Now there is only one form to read."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    tool(call_id, "verify_identity", date_of_birth="1965-02-28")
    status = tool(call_id, "get_claim_status")
    claim = status["primary_claim"]

    assert "amount" not in claim, "the figures must not be reachable"
    assert claim["amount_spoken"] == "four thousand two hundred and fifty dollars"
    assert "$" not in str(status), "no figure form anywhere in the payload"
    # Dates in the narrative fields are spoken too — "October 12th" was read
    # out as "October 12" on the same call.
    assert claim["next_step"].startswith("Payment was issued the twelfth of October")
    assert claim["status_detail"] == "Approved in full on the tenth of October."


def test_a_second_claim_is_mentioned_not_recited():
    """A live call read out every field of both of Daniel's claims."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    tool(call_id, "verify_identity", date_of_birth="1979-11-03")
    status = tool(call_id, "get_claim_status")

    assert status["claim_count"] == 2
    # The open claim leads; the paid one is secondary.
    assert status["primary_claim"]["claim_id"] == "CLM-48201"
    assert [c["claim_id"] for c in status["other_claims"]] == ["CLM-48118"]
    assert "Do not recite the details of more than one claim" in status["instruction"]


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


def test_adding_a_photo_routes_to_document_submission():
    """A live call asked this and the KB matched "How to start a new claim" at
    55.9 — above MIN_SCORE, and not an answer to the question. The model then
    substituted its own ("contact your adjuster"), which is nowhere in the KB
    and points the caller at the slowest channel."""
    for question in ("How to add a photo to an existing claim?",
                     "what do I do if I want to add a photo later",
                     "can I send more photos"):
        result = tool(new_call(), "search_knowledge_base", query=question)
        assert result["found"] is True, question
        assert result["topic"] == "How to submit documents", question
        # Spoken form: the portal is still the channel it points at.
        assert "observeinsurance dot com slash upload" in result["answer"]


def test_a_confident_match_still_tells_the_agent_to_check_it_fits():
    """MIN_SCORE filters weak matches; nothing filters a confident wrong one."""
    result = tool(new_call(), "search_knowledge_base", query="what are your office hours")
    assert result["found"] is True
    instruction = result["instruction"]
    assert "can still be the wrong topic" in instruction
    # The near-miss must land on the refusal path, not on an invented answer.
    assert "do not substitute an answer of your own" in instruction
    assert "representative" in instruction


@pytest.mark.parametrize(
    "question",
    [
        "who is the president of the united states",
        "who is the prime minister of the united states",
        "what is the capital of france",
    ],
)
def test_a_general_knowledge_question_is_refused_not_answered(question):
    """A live call answered this from the model's own weights — "As of October
    2023, the President of the United States is Joe Biden" — on a branded
    insurance line. The refusal threshold is the defence, so the instruction it
    returns has to close off answering from memory, not just guessing."""
    result = tool(new_call(), "search_knowledge_base", query=question)
    assert result["found"] is False
    instruction = result["instruction"].lower()
    assert "from your own knowledge" in instruction
    assert "representative" in instruction


def test_an_empty_query_still_tells_the_agent_what_to_do():
    result = tool(new_call(), "search_knowledge_base", query="")
    assert result["found"] is False
    assert result["reason"] == "empty_query"
    assert "own knowledge" in result["instruction"]


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
    instruction = result["instruction"]
    assert "someone can help" in instruction          # a human is still offered
    assert "not that you will connect or transfer" in instruction


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


# --- the callback number -------------------------------------------------
# A live call promised "a representative will call you back on this number" to
# a caller who had no caller ID and could not remember the number on their
# policy. They read one out, the agent thanked them for it, and it reached no
# field a human could act on — only the transcript.
def test_a_callback_with_no_number_tells_the_agent_to_ask_for_one(monkeypatch):
    from app import config

    config.settings.cache_clear()
    monkeypatch.setenv("BUSINESS_HOURS_MODE", "closed")
    try:
        result = tool(new_call(), "escalate_to_human", reason="customer_not_found")
        assert result["transfer"] == "callback"
        assert result["callback_number"] == ""
        instruction = result["instruction"].lower()
        assert "record_callback_number" in instruction
        assert "do not end the call" in instruction
    finally:
        monkeypatch.delenv("BUSINESS_HOURS_MODE", raising=False)
        config.settings.cache_clear()


def test_the_callback_number_reaches_the_handoff_and_the_record():
    call_id = new_call()
    escalation = tool(call_id, "escalate_to_human", reason="customer_not_found")
    ticket = escalation["ticket_id"]

    recorded = tool(call_id, "record_callback_number", callback_number="(807) 259-3908")
    assert recorded["ok"] is True
    assert recorded["callback_number"] == "8072593908"
    assert recorded["ticket_id"] == ticket  # attached to the open handoff

    end_call(call_id, "Caller could not be identified; a callback was arranged.")
    written = repository().interactions[call_id]
    assert written.callback_number == "8072593908"
    # Never the field a CRM matches a contact on: the number is unverified and
    # may not be the one on the policy.
    assert written.caller_phone == ""


def test_a_callback_number_short_of_ten_digits_is_read_back_not_saved():
    call_id = new_call()
    result = tool(call_id, "record_callback_number", callback_number="807 259")
    assert result["error"] == "invalid_phone"
    assert result["digits_heard"] == 6
    assert store.peek(call_id).callback_number == ""


def test_a_volunteered_number_is_captured_by_the_escalation_itself():
    call_id = new_call()
    escalation = tool(call_id, "escalate_to_human", reason="caller_request",
                      callback_number="807-259-3908")
    assert escalation["handoff_context"]["callback_number"] == "8072593908"


def test_the_handoff_carries_the_caller_id_when_no_number_was_volunteered():
    """A live ticket went to Slack with callback_number "" while the result
    beside it carried the caller's number. The message was still correct —
    the notifier falls back to caller_phone — but the logged handoff showed a
    blank where a human was looking for a number to ring."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    escalation = tool(call_id, "escalate_to_human", reason="authentication_failed")
    assert escalation["handoff_context"]["callback_number"] == "5554184492"
    assert escalation["callback_number"] == "5554184492"  # and the two agree


def test_confirming_the_number_already_on_the_ticket_does_not_notify_again(monkeypatch):
    """The escalation says to call this tool if the caller gives a *different*
    number. On a live call the caller confirmed the one read back to them and
    the model called it anyway, posting a second Slack message telling a human
    to ring a number already on the card."""
    from app.tools import escalation

    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(escalation, "notify_callback_number",
                        lambda ctx: posted.append(ctx) is None)

    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    tool(call_id, "escalate_to_human", reason="authentication_failed")

    same = tool(call_id, "record_callback_number", callback_number="555-418-4492")
    assert same["ok"] is True
    assert same["notified"] is False
    assert posted == []  # nothing was even handed to the notifier
    assert "do not read it back again" in same["instruction"].lower()

    # A genuinely new number still reaches the human.
    changed = tool(call_id, "record_callback_number", callback_number="807-259-3908")
    assert changed["notified"] is True
    assert [p["callback_number"] for p in posted] == ["8072593908"]
    assert "read the number back" in changed["instruction"].lower()


def test_lockout_does_not_let_the_agent_promise_a_transfer():
    """A live call heard "I'll transfer you to someone who can help" and then,
    once `escalate_to_human` answered, "the team is currently closed". The
    lockout instruction said "hand the caller to a representative", which the
    model spoke as a promise — before the tool that decides had run."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    for dob in ("1970-01-01", "1971-01-01", "1972-01-01"):
        locked = tool(call_id, "verify_identity", date_of_birth=dob)
    instruction = locked["instruction"].lower()

    assert "escalate_to_human" in instruction
    # The old wording. "Hand the caller to a representative" is what the model
    # spoke as a promise; the word "transfer" still appears here, but only
    # inside the counter-example telling it not to.
    assert "hand the caller to a representative" not in instruction
    for directive in ("transfer the call", "transfer them", "transfer the caller"):
        assert directive not in instruction
    # The point is the ordering, not the wording: nothing about what happens
    # next may be said until the tool has decided what is possible.
    assert "until that tool answers" in instruction


def test_no_tool_asks_the_model_for_its_own_goodbye():
    """Vapi's endCallMessage is the only sign-off. A live call heard two:
    "Thank you for calling Observe Insurance. Have a great day! Goodbye!" and
    then "Thanks for calling Observe Insurance. Take care." — because this
    tool's instruction asked for one on top of the configured one."""
    result = tool(new_call(), "log_interaction", summary="Done.", sentiment="neutral")
    instruction = result["instruction"]
    assert "Do not say goodbye" in instruction
    assert "then say goodbye" not in instruction


def test_no_tool_offers_a_connection_it_cannot_make():
    """Only `escalate_to_human` knows whether this is a live transfer or a
    callback tomorrow. A live call had triage say "I can connect you to a rep"
    and the escalation agent walk it back seconds later, because the team was
    closed. Any tool that offers a human before that is decided has to offer
    the help, not the mechanism."""
    call_id = new_call()
    missed = tool(call_id, "lookup_customer", phone_number="555-999-8888")
    assert missed["found"] is False
    assert "someone can help" in missed["instruction"]
    assert "transferred to a representative" not in missed["instruction"]


# --- the escalation reason code ------------------------------------------
def test_customer_not_found_is_corrected_when_nothing_was_looked_up():
    """A live call escalated `customer_not_found` for a caller who could not
    remember their number. Nothing was ever searched, so nothing was missing —
    a different problem with a different fix, and it skews every count built
    on the reason field."""
    call_id = new_call()
    escalation = tool(call_id, "escalate_to_human", reason="customer_not_found",
                      notes="Cannot remember the number on the account.")
    context = escalation["handoff_context"]
    assert context["reason"] == "no_identifier_given"
    # The correction is visible, not silent: the notes were written to fit the
    # code the agent reported.
    assert context["reason_reported"] == "customer_not_found"
    assert store.peek(call_id).escalation_reason == "no_identifier_given"


def test_customer_not_found_stands_when_a_lookup_really_missed():
    call_id = new_call()
    missed = tool(call_id, "lookup_customer", phone_number="555-999-8888")
    assert missed["found"] is False

    context = tool(call_id, "escalate_to_human",
                   reason="customer_not_found")["handoff_context"]
    assert context["reason"] == "customer_not_found"
    assert context["reason_reported"] == ""


def test_an_unverified_account_holder_is_not_reported_as_not_found():
    """The account exists; identity was simply never confirmed."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    context = tool(call_id, "escalate_to_human",
                   reason="customer_not_found")["handoff_context"]
    assert context["reason"] == "verification_incomplete"


def test_authentication_failed_needs_the_attempts_to_back_it_up():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    context = tool(call_id, "escalate_to_human",
                   reason="authentication_failed")["handoff_context"]
    assert context["reason"] == "verification_incomplete"

    locked = new_call()
    tool(locked, "lookup_customer", phone_number="555-418-4492")
    for _ in range(3):
        tool(locked, "verify_identity", date_of_birth="1965-02-27")
    held = tool(locked, "escalate_to_human",
                reason="authentication_failed")["handoff_context"]
    assert held["reason"] == "authentication_failed"
    assert held["reason_reported"] == ""


def test_a_reason_only_the_model_witnessed_is_taken_at_its_word():
    """`distressed_caller` is not checkable server-side, so it is not checked."""
    context = tool(new_call(), "escalate_to_human",
                   reason="distressed_caller")["handoff_context"]
    assert context["reason"] == "distressed_caller"
    assert context["reason_reported"] == ""


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


def wait_for_record(call_id: str, timeout: float = 2.0):
    """`log_interaction` writes on a worker thread so a caller never waits on it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = repository().interactions.get(call_id)
        if record is not None:
            return record
        time.sleep(0.01)
    raise AssertionError(f"no interaction record was written for {call_id}")


def test_the_end_of_call_report_cannot_downgrade_a_logged_outcome():
    """A live call logged `callback_scheduled`, then got relabelled `pending`.

    Vapi's squad-mode structuredData carried no outcome field, so the webhook
    fell through to the session's auth state — and because the Salesforce
    subject is derived from the outcome and sent on every write, the second,
    poorer write won.
    """
    call_id = new_call()
    tool(call_id, "escalate_to_human", reason="customer_not_found")
    tool(call_id, "log_interaction", summary="Callback arranged.", sentiment="neutral",
         outcome="callback_scheduled")
    assert wait_for_record(call_id).outcome == "callback_scheduled"

    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        # Vapi's own default schema: a summary and a sentiment, no outcome.
        "analysis": {"summary": "Caller could not be identified.",
                     "structuredData": {"sentiment": "neutral"}},
    }})
    assert repository().interactions[call_id].outcome == "callback_scheduled"


def test_pending_auth_is_never_written_as_an_outcome():
    """`pending` is where a session starts, not how a call ended."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    end_call(call_id, "Caller hung up during verification.")
    assert repository().interactions[call_id].outcome == ""


def test_intent_falls_back_to_vapis_own_structured_fields():
    """The call with no server tool at all is the one with the least other
    information about it, and the intent field landed empty on exactly those."""
    call_id = new_call()
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {
            "summary": "Caller asked a general question.",
            # Vapi's squad-mode default schema: no `intent` key anywhere.
            "structuredData": {"call_type": "Claims Support",
                               "issue_type": "Claim Status Inquiry"},
        },
    }})
    # The specific key wins over the generic one, in the session's own format.
    assert repository().interactions[call_id].intent == "claim_status_inquiry"


def test_the_session_still_outranks_the_providers_extraction():
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {"summary": "Caller checked a claim.",
                     "structuredData": {"call_type": "Claims Support"}},
    }})
    assert repository().interactions[call_id].intent == "authentication"


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


def test_the_verification_confirmation_survives_the_handoff_too():
    """A live call went from a date of birth straight to claim details.

    `verify_identity` returned "thank the caller by name", triage transferred
    580ms later without speaking, and the specialist is told never to greet —
    so the caller was never told the security check had passed. The duty is
    re-issued to whichever agent actually speaks.
    """
    call_id = new_call()
    tool_as(call_id, "observe-triage", "lookup_customer", phone_number="555-234-1187")
    verified = tool_as(call_id, "observe-triage", "verify_identity", date_of_birth="1979-11-03")
    # The instruction may not read as "greet them, or else get on with it".
    assert "before anything else" in verified["instruction"].lower()
    assert "Daniel" in verified["instruction"]

    status = tool_as(call_id, "observe-claims-specialist", "get_claim_status")
    instruction = status["instruction"]
    assert instruction.startswith("The caller was verified a moment ago")
    assert "Daniel" in instruction
    # Said once. A second claim lookup must not ask for it again.
    again = tool_as(call_id, "observe-claims-specialist", "get_claim_status")
    assert not again["instruction"].startswith("The caller was verified a moment ago")


def test_one_assistant_doing_everything_is_not_told_to_re_confirm():
    """The reminder exists because a handoff intervened. Without one, the agent
    that was told to say it is the agent that speaks, and repeating the duty
    would just invite it to say the same thing twice."""
    call_id = new_call()
    tool_as(call_id, "observe-claims-agent", "lookup_customer", phone_number="555-234-1187")
    tool_as(call_id, "observe-claims-agent", "verify_identity", date_of_birth="1979-11-03")
    status = tool_as(call_id, "observe-claims-agent", "get_claim_status")
    assert not status["instruction"].startswith("The caller was verified a moment ago")


def test_every_tool_event_records_which_agent_made_it():
    """Diagnosing the dropped confirmation meant leaving this console for the
    provider's call record, because the session knew the agents involved but
    not which one made which call."""
    call_id = new_call()
    tool_as(call_id, "observe-triage", "lookup_customer", phone_number="555-234-1187")
    tool_as(call_id, "observe-triage", "verify_identity", date_of_birth="1979-11-03")
    tool_as(call_id, "observe-claims-specialist", "get_claim_status")

    events = store.peek(call_id).snapshot()["tool_events"]
    assert [(e["name"], e["assistant"]) for e in events] == [
        ("lookup_customer", "observe-triage"),
        ("verify_identity", "observe-triage"),
        ("get_claim_status", "observe-claims-specialist"),
    ]


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


def test_the_bare_domain_lands_on_the_console():
    """`{"detail":"Not Found"}` at / reads as a broken deploy to anyone who
    opens the URL, even though it is the correct answer for an API."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/debug"
    assert client.get("/").status_code == 200


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


# --- knowledge base: written for a screen, spoken down a phone -------------
def test_an_email_address_and_a_fax_number_are_rewritten_for_the_voice():
    """A live call read claims@observeinsurance.com as "claims observe
    insurance dot com" — the @ vanished, leaving an address that does not
    exist — and dropped the fax number entirely partway through the sentence.
    """
    from app.knowledge import speakable

    answer = tool(new_call(), "search_knowledge_base",
                  query="how do I send photos")["answer"]
    assert "@" not in answer
    assert "claims at observeinsurance dot com" in answer
    assert "9 1 6, 5 5 5, 0 1 8 8" in answer
    assert "observeinsurance dot com slash upload" in answer

    # The transform itself, independent of any one entry.
    assert speakable("mail a@b.com") == "mail a at b dot com"
    assert speakable("call 916-555-0188") == "call 9 1 6, 5 5 5, 0 1 8 8"
    # Prose is left alone.
    assert speakable("Approved payments issue in three to five days.") == (
        "Approved payments issue in three to five days.")


def test_a_long_answer_is_told_to_stop_rather_than_recite():
    """Twenty-three seconds of unbroken speech on a live call, to answer
    "how do I add a photo later"."""
    instruction = tool(new_call(), "search_knowledge_base",
                       query="how do I send photos")["instruction"].lower()
    assert "do not read the whole list" in instruction
    assert "stop and let them ask" in instruction


def test_document_channels_are_spoken_not_printed():
    """Same strings as the knowledge base, reached through a different tool.
    Fixing only the KB left this one able to read out an email address with
    no "at" in it."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-361-2234")
    tool(call_id, "verify_identity", date_of_birth="1988-04-12")
    docs = tool(call_id, "get_document_instructions")

    details = " ".join(c["detail"] for c in docs["channels"])
    assert "@" not in details
    assert "claims at observeinsurance dot com" in details
    assert "9 1 6, 5 5 5, 0 1 8 8" in details
    assert "megabytes" in docs["accepted_formats"]


@pytest.mark.parametrize(
    "written,spoken",
    [
        ("2026-10-29", "the twenty-ninth of October"),
        ("Payment was issued October 12th.", "Payment was issued the twelfth of October."),
        ("Approved on October 10th.", "Approved on the tenth of October."),
        ("Reimbursed on September 28th.", "Reimbursed on the twenty-eighth of September."),
        ("the inspection is on October 1st", "the inspection is on the first of October"),
        # Not a date: a year must not become a day.
        ("renewed in October 2026", "renewed in October 2026"),
        ("", ""),
    ],
)
def test_dates_are_rendered_for_a_voice_not_a_screen(written, spoken):
    """TTS read "October 12th" as "October 12", and an earlier bug turned
    "October 29th" into "October 20 ninth"."""
    from app.models import speakable

    assert speakable(written) == spoken


def test_the_verification_instruction_never_mentions_a_handoff():
    """It used to say "before any other tool and before any handoff", and the
    agent announced the transfer it is forbidden to mention: "You're all
    verified. Let me transfer you to a claims specialist." Naming a thing to
    rule it out still puts it in the room."""
    call_id = new_call()
    tool_as(call_id, "observe-triage", "lookup_customer", phone_number="555-418-4492")
    verified = tool_as(call_id, "observe-triage", "verify_identity",
                       date_of_birth="1965-02-28")
    instruction = verified["instruction"].lower()

    for word in ("handoff", "hand off", "transfer", "specialist", "colleague"):
        assert word not in instruction, f"{word!r} must not be put in the model's head here"
    assert "Marcus" in verified["instruction"]

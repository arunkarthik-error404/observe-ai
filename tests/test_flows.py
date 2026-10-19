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
    store.reset()
    yield
    repository().reset()
    store.reset()


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

    # One call, not two. The documents ride back with the status that says they
    # are needed, because the server already had them when it answered.
    docs = status["documents"]
    assert "plumber" in docs["required_documents"]
    assert docs["deadline"] == "the twenty-ninth of October"  # spoken, not ISO
    assert docs["channels"][0]["channel"] == "portal"
    assert "recommend the portal" in status["instruction"]


def test_a_claim_needing_nothing_carries_no_document_block():
    """The block is the signal. Sending an empty one on every call would make
    the model announce paperwork to a caller who owes none."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    tool(call_id, "verify_identity", date_of_birth="1979-11-03")

    status = tool(call_id, "get_claim_status")
    assert "documents" not in status
    assert "recommend the portal" not in status["instruction"]


def test_documents_are_found_on_a_second_claim_too():
    """The focus claim is the first open one, which is not necessarily the one
    waiting on paperwork."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-361-2234")
    tool(call_id, "verify_identity", date_of_birth="1988-04-12")

    status = tool(call_id, "get_claim_status")
    assert status["documents"]["claim_id"] == "CLM-48255"


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
        assert "observeinsurance dot com, forward slash, upload" in result["answer"]


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
    assert "no goodbye" in instruction
    assert "then say goodbye" not in instruction
    # Naming the sentence that will play beats forbidding the category: the
    # model can hear this one as already spoken, rather than having to
    # remember a rule about it.
    assert "Thanks for calling Observe Insurance. Take care." in instruction
    assert "automatic" in instruction


def test_a_miss_tells_the_agent_how_to_walk_back_a_premature_hit():
    """A live call announced "I found an account with that number" 2.8s before
    the lookup returned, in the same completion as the tool call. The number
    happened to exist. On an unknown number the same sentence invents cover the
    caller does not have, and this is the one path where the server can see
    that contradiction and name the recovery."""
    missed = tool(new_call(), "lookup_customer", phone_number="555-999-8888")
    instruction = missed["instruction"].lower()

    assert missed["found"] is False
    assert "if you already told them an account was found, correct it now" in instruction
    # Still no date of birth: asking would confirm the account exists.
    assert "do not ask for a date of birth" in instruction


def test_the_quoted_signoff_is_the_one_the_config_actually_speaks():
    """`log_interaction` quotes the sign-off verbatim so the model treats it as
    already said. That only works while the quote matches `endCallMessage` —
    edit one without the other and the tool is telling the model the caller
    will hear words the caller will not hear."""
    import json
    configured = json.load(open("vapi/agents.json", encoding="utf-8"))["defaults"]["endCallMessage"]
    instruction = tool(new_call(), "log_interaction", summary="Done.",
                       sentiment="neutral")["instruction"]

    assert configured in instruction


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


# --- sentiment: the field the PDF names and nothing used to own ------------
def test_sentiment_is_not_inferred_from_the_assistants_own_success_grade():
    """`successEvaluation` asks whether *we* followed policy; sentiment asks
    about the caller's tone. Mapping one onto the other inverted the case that
    matters most — an angry caller correctly escalated is the assistant
    succeeding, and used to be stored as `positive`."""
    call_id = new_call()
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {
            "summary": "Third call about the same delayed claim; escalated.",
            "successEvaluation": "true",
            # The squad shape: Vapi substituted its own schema, so no spelling
            # of sentiment is present.
            "structuredData": {"call_type": "Claims Support", "issue_type": "claim delay"},
        },
    }})
    assert repository().interactions[call_id].sentiment == "neutral"


def test_a_failed_success_grade_does_not_become_a_negative_caller():
    """The mirror: a calm caller whose identity could not be matched was
    stored as `negative` because the policy check failed."""
    call_id = new_call()
    client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {"summary": "Could not verify identity.", "successEvaluation": "false",
                     "structuredData": {"call_type": "Claims Support"}},
    }})
    assert repository().interactions[call_id].sentiment == "neutral"


def test_sentiment_is_constrained_to_the_configured_vocabulary():
    """`agents.json` commits to four values in both the analysis schema and the
    `log_interaction` tool, and nothing server-side enforced it — so "Positive "
    and "MIXED" were stored verbatim and fragmented every aggregate."""
    from app.models import SENTIMENTS, InteractionRecord

    for raw, expected in [("Positive ", "positive"), ("NEGATIVE", "negative"),
                          ("very frustrated", "frustrated"), ("angry", "frustrated"),
                          ("mixed", "neutral"), ("", "neutral")]:
        assert InteractionRecord(call_id="c", sentiment=raw).sentiment == expected

    # Both write paths go through the model, so neither can store an off-enum value.
    for path in ("end-of-call-report", "in-call-tool"):
        record = InteractionRecord(call_id="c", sentiment="fuchsia", source=path)
        assert record.sentiment in SENTIMENTS


def test_an_unknown_sentiment_never_fails_the_write():
    """A ValidationError here would 500 the end-of-call webhook, and Vapi
    retries a non-2xx — so a mislabelled tone would cost the whole record."""
    call_id = new_call()
    response = client.post("/vapi/webhook", json={"message": {
        "type": "end-of-call-report",
        "call": {"id": call_id},
        "analysis": {"summary": "Routine call.",
                     "structuredData": {"sentiment": "cautiously optimistic"}},
    }})
    assert response.status_code == 200
    assert repository().interactions[call_id].sentiment == "neutral"


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
    """The case for having a second agent at all rests entirely on this.

    Vapi keeps one call id across a squad transfer and the session is keyed on
    that id, so the escalation agent inherits the verification the claims
    agent performed instead of putting the caller through it twice — at the
    worst possible moment, having just asked for a person.

    This test used to prove it across a triage -> specialist handoff. That
    handoff is gone: it fired immediately after verification with no caller
    turn in between, so it split one continuous exchange and bought ~8s of
    dead air. The remaining transfer is the one that was always carrying
    something.
    """
    call_id = new_call()

    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")
    verified = tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1979-11-03")
    assert verified["verified"] is True
    tool_as(call_id, "observe-claims", "get_claim_status")

    # ... handoff happens here, the escalation agent now holds the call ...
    escalation = tool_as(call_id, "observe-escalation", "escalate_to_human",
                         reason="caller_request")
    assert escalation["handoff_context"]["identity_verified"] is True
    assert escalation["handoff_context"]["customer_name"] == "Daniel Okafor"

    session = store.peek(call_id)
    assert session.assistant_path == ["observe-claims", "observe-escalation"]
    assert session.customer.full_name == "Daniel Okafor"


def test_the_confirmation_is_owed_by_the_agent_that_stays():
    """Two live calls had the squad transfer sever this sentence mid-word —
    "You're all" — because `verify_identity` asked the agent that was about to
    hand the call on to say it. The agent reading the next tool's instruction
    is always one that is staying."""
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")
    verified = tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1979-11-03")

    # Nothing to start saying, so nothing to be cut off.
    assert "do not announce it yet" in verified["instruction"].lower()
    assert "Daniel" not in verified["instruction"]

    status = tool_as(call_id, "observe-claims", "get_claim_status")
    assert status["instruction"].startswith("First, before anything below")
    assert "Thanks, Daniel, you're all verified" in status["instruction"]
    # The model read a stray closing quote aloud: "You're all verified.'"
    assert '"' not in status["instruction"].split("Nobody has told them")[0]


def test_the_confirmation_is_owed_exactly_once():
    """When `verify_identity` and the next tool both asked for it, the caller
    heard their own name twice."""
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")
    tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1979-11-03")

    first = tool_as(call_id, "observe-claims", "get_claim_status")
    assert first["instruction"].startswith("First, before anything below")
    for _ in range(3):
        again = tool_as(call_id, "observe-claims", "get_claim_status")
        assert not again["instruction"].startswith("First, before anything below")


def test_the_confirmation_waits_for_the_next_tool_not_the_next_agent():
    """It was never a handoff repair, which is why it survived the handoff
    being removed: the same agent simply says it a moment later, on the next
    tool instead of on `verify_identity`. That ordering is also what stops it
    being said before the check has actually run."""
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")
    tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1979-11-03")
    status = tool_as(call_id, "observe-claims", "get_claim_status")
    assert status["instruction"].startswith("First, before anything below")


def test_a_verified_caller_who_only_asks_a_question_is_still_told():
    """They may never reach a claims tool at all."""
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")
    tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1979-11-03")
    faq = tool_as(call_id, "observe-claims", "search_knowledge_base",
                  query="what are your office hours")
    assert faq["instruction"].startswith("First, before anything below")


def test_the_document_deadline_is_not_turned_back_into_figures():
    """The rule used to live only on get_claim_status, so a caller who went
    straight to documents heard "the 20 ninth of October" and then "29
    October". Now both halves arrive together and so does the rule."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-361-2234")
    tool(call_id, "verify_identity", date_of_birth="1988-04-12")
    status = tool(call_id, "get_claim_status")

    assert status["documents"]["deadline"] == "the twenty-ninth of October"
    assert "never 29 October or October 29th" in status["instruction"]


def test_every_tool_event_records_which_agent_made_it():
    """Diagnosing the dropped confirmation meant leaving this console for the
    provider's call record, because the session knew the agents involved but
    not which one made which call."""
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")
    tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1979-11-03")
    tool_as(call_id, "observe-claims", "get_claim_status")

    events = store.peek(call_id).snapshot()["tool_events"]
    assert [(e["name"], e["assistant"]) for e in events] == [
        ("lookup_customer", "observe-claims"),
        ("verify_identity", "observe-claims"),
        ("get_claim_status", "observe-claims"),
    ]


def test_a_premature_handoff_still_cannot_read_claim_data():
    """Capability scoping is the first control; the session gate is the second.

    If triage ever transferred before verifying, the specialist has the tool
    but the server still refuses — which is why the squad is defence in depth
    rather than the thing the security rests on.
    """
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-234-1187")

    # The escalation agent is the only other agent there is, and it does not
    # hold this tool — but the gate is the server's, not the tool list's.
    blocked = tool_as(call_id, "observe-escalation", "get_claim_status")
    assert blocked["error"] == "not_authenticated"
    # And the documents that now ride with the status are behind the same gate.
    assert "documents" not in blocked


def test_escalation_agent_sees_everything_the_earlier_agents_learned():
    call_id = new_call()
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-693-6678")
    tool_as(call_id, "observe-claims", "verify_identity", date_of_birth="1982-01-09")
    tool_as(call_id, "observe-claims", "get_claim_status")

    escalation = tool_as(call_id, "observe-escalation", "escalate_to_human",
                         reason="caller_request", notes="Wants to appeal with a person.")
    context = escalation["handoff_context"]
    assert context["customer_name"] == "Ana Duarte"
    assert context["identity_verified"] is True
    assert "CLM-48402" in context["claims_discussed"]

    path = store.peek(call_id).assistant_path
    assert path == ["observe-claims", "observe-escalation"]


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
    assert "observeinsurance dot com, forward slash, upload" in answer

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
    docs = tool(call_id, "get_claim_status")["documents"]

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
    tool_as(call_id, "observe-claims", "lookup_customer", phone_number="555-418-4492")
    verified = tool_as(call_id, "observe-claims", "verify_identity",
                       date_of_birth="1965-02-28")
    instruction = verified["instruction"].lower()

    for word in ("handoff", "hand off", "transfer", "specialist", "colleague"):
        assert word not in instruction, f"{word!r} must not be put in the model's head here"
    # And no name either: the sentence itself has moved to the next tool, so
    # there is nothing here to begin saying and be cut off halfway through.
    assert "Marcus" not in verified["instruction"]


def test_nothing_offers_to_send_the_caller_anything():
    """The agent asked "would you like me to send you the upload link?", the
    caller said yes, and it answered "I can't send the upload link directly".
    There is no tool that sends anything, and three places used to instruct
    exactly that offer."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-361-2234")
    tool(call_id, "verify_identity", date_of_birth="1988-04-12")
    instruction = tool(call_id, "get_claim_status")["instruction"].lower()
    assert "offer to text or email" not in instruction
    assert "cannot send, text or email anything" in instruction

    # And no prompt may reinstate it — every prompt on disk, so a new agent
    # cannot be added without this applying to it.
    import pathlib
    for path in pathlib.Path("vapi/prompts").glob("*.md"):
        text = path.read_text(encoding="utf-8").lower()
        assert "offer to text or email" not in text, path.name


def test_the_claim_instruction_names_the_figure_forms_it_forbids():
    """Dropping `amount` from the payload was not enough — the model wrote the
    words back as digits, which two calls with identical payload text proved
    by rendering one date each way."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    tool(call_id, "verify_identity", date_of_birth="1965-02-28")
    instruction = tool(call_id, "get_claim_status")["instruction"]

    assert "never \"4250 dollars\"" in instruction
    assert "never \"12 October\"" in instruction
    # The stale key it used to name is gone along with the field itself.
    assert "never amount" not in instruction.lower()


def test_a_url_path_survives_being_spoken():
    """"slash" was dropped twice on live calls, leaving "observeinsurance dot
    com upload" — an address the caller cannot type. A one-syllable function
    word mid-phrase is the easiest thing in a sentence to swallow."""
    from app.models import speakable

    assert speakable("go to observeinsurance.com/upload") == (
        "go to observeinsurance dot com, forward slash, upload")
    # A bare domain is left as a domain.
    assert speakable("visit observeinsurance.com") == "visit observeinsurance dot com"


def test_instant_tools_tell_the_model_not_to_announce_them():
    """Three live calls said "this will just take a sec" in front of tools that
    answer in under ten milliseconds. Two prompt sections failed to stop it, so
    the rule moves onto the tool descriptions — what the model reads at the
    moment it decides to call."""
    import json

    cfg = json.load(open("vapi/agents.json", encoding="utf-8"))
    for name, spec in cfg["tools"].items():
        if name.startswith("_"):
            continue
        description = spec["description"]
        if spec.get("request_start"):
            # These two reach the claims system and Vapi speaks a line itself;
            # telling the model to stay silent would contradict what the caller
            # is already hearing.
            assert "say nothing before calling it" not in description, name
        else:
            assert "say nothing before calling it" in description, name


def test_the_last_tool_before_hangup_forbids_a_parting_filler():
    """`log_interaction` runs immediately before the call ends, which makes it
    the last thing that can be said to the model before "Give me a moment." and
    then the sign-off."""
    call_id = new_call()
    instruction = tool(call_id, "log_interaction", outcome="status_provided",
                       summary="Checked a claim.", sentiment="neutral")["instruction"].lower()
    assert "say nothing at all before you do" in instruction
    assert "nobody is waiting" in instruction
    for filler in ("just a sec", "one moment", "give me a moment"):
        assert filler in instruction, filler


def test_the_closing_question_is_owed_before_the_record_not_after_it():
    """The ordering is the fix for two opposite live failures: a filler in
    front of the closing question, and a hang-up seven tenths of a second
    after this tool answered with the question never asked. Leaving the model
    one action and no speech is what removes the window."""
    call_id = new_call()
    instruction = tool(call_id, "log_interaction", outcome="status_provided",
                       summary="Checked a claim.", sentiment="neutral")["instruction"].lower()

    assert "closing question is already behind you" in instruction
    # And it must not re-open the window by asking for the question again.
    assert "ask if there is anything else" not in instruction


# --- throttling that outlives the call ------------------------------------
def test_guessing_is_not_reset_by_hanging_up_and_redialling(monkeypatch):
    """`auth_attempts` lives on CallSession, which dies with the call. Three
    wrong dates of birth, a redial, and three more was unlimited guessing
    against a known number."""
    from app import config

    config.settings.cache_clear()
    monkeypatch.setenv("MAX_ATTEMPTS_PER_NUMBER", "5")
    try:
        burned = 0
        for _ in range(3):                       # three separate calls
            call_id = new_call()
            tool(call_id, "lookup_customer", phone_number="555-418-4492")
            for year in (1970, 1971, 1972):
                result = tool(call_id, "verify_identity", date_of_birth=f"{year}-01-01")
                if result.get("locked") and result["instruction"].startswith("This account"):
                    # The number ran out of budget, mid-call, on a fresh session.
                    assert burned >= 5
                    assert "how many attempts" in result["instruction"]
                    return
                burned += 1
        raise AssertionError("a redial handed out a clean slate")
    finally:
        monkeypatch.delenv("MAX_ATTEMPTS_PER_NUMBER", raising=False)
        config.settings.cache_clear()


def test_verifying_successfully_forgives_the_number(monkeypatch):
    """Otherwise a caller who fumbles twice and then gets it right is counted
    against for the next hour."""
    from app import config
    from app.session import store as session_store

    config.settings.cache_clear()
    try:
        call_id = new_call()
        tool(call_id, "lookup_customer", phone_number="555-418-4492")
        tool(call_id, "verify_identity", date_of_birth="1970-01-01")
        assert session_store.attempts_for_number("5554184492") == 1

        assert tool(call_id, "verify_identity", date_of_birth="1965-02-28")["verified"] is True
        assert session_store.attempts_for_number("5554184492") == 0
    finally:
        config.settings.cache_clear()


def test_the_ticket_id_is_stable_across_restarts():
    """Python salts string hashing per process, so the same call produced
    ESC-4982, ESC-3744 and ESC-6060 on three runs — an id that changes if the
    server restarts between a caller writing it down and a human looking it
    up."""
    import hashlib

    call_id = new_call()
    ticket = tool(call_id, "escalate_to_human", reason="caller_request")["ticket_id"]
    expected = int(hashlib.sha256(call_id.encode()).hexdigest()[:8], 16) % 9000 + 1000
    assert ticket == f"ESC-{expected}"
    assert len(ticket) == len("ESC-0000")  # still four digits, still sayable


# --- retrieval: confident, and ambiguous ----------------------------------
def test_two_topics_matching_equally_produces_a_question_not_a_guess():
    """A single threshold can say "weak" but not "confident, and ambiguous".
    "how do i send you the paper work" scores 57.8 against both Mailing
    address and How to submit documents, and picking the first of two equals
    presents a coin toss to the caller as an answer."""
    result = tool(new_call(), "search_knowledge_base",
                  query="how do i send you the paper work")

    assert result["found"] is False
    assert result["reason"] == "ambiguous"
    assert result["margin"] < 8.0
    assert set(result["options"]) == {"Mailing address", "How to submit documents"}
    instruction = result["instruction"].lower()
    assert "do not pick one" in instruction
    assert "ask them which they mean" in instruction
    # Asked in the caller's words, not ours.
    assert "in plain words rather than these titles" in instruction


def test_a_clear_winner_is_still_answered_outright():
    """The clarification branch has to be surgical. If it fires on ordinary
    questions it has traded one failure for a more annoying one."""
    result = tool(new_call(), "search_knowledge_base", query="how do I upload my photos")
    assert result["found"] is True
    assert result["topic"] == "How to submit documents"
    assert result["margin"] >= 8.0


def test_the_keyword_override_is_visible_as_itself():
    """A keyword hit forced to 92.0 and a genuine fuzzy match at 92.0 were
    indistinguishable downstream, which matters now that the margin check
    reads those scores: two keyword hits tie at 92 and are genuinely
    ambiguous, while a keyword hit over a weak field is not."""
    result = tool(new_call(), "search_knowledge_base", query="what are your office hours")
    assert result["found"] is True
    top = result["candidates"][0]
    assert top["exact_keyword"] is True
    assert top["score"] == 92.0


# --- provenance -----------------------------------------------------------
def test_an_answer_carries_the_document_it_came_from():
    """"Where did the agent get that from" is a compliance question in
    insurance, not a nicety. The citation is never spoken — it travels to the
    call record and the escalation ticket so a human can check the agent
    against the source instead of taking its word."""
    call_id = new_call()
    result = tool(call_id, "search_knowledge_base", query="what are your office hours")

    assert result["source"] == "claims-handbook-2026-v3 §1.1"
    assert result["owner"] == "claims-ops"
    # Per-entry length budget, rather than one blanket rule for a four-channel
    # list and a one-line answer alike.
    assert "Keep it to 2 sentence(s) at most" in result["instruction"]

    # It reaches the handoff, and the post-call record.
    handoff = tool(call_id, "escalate_to_human", reason="caller_request")["handoff_context"]
    assert handoff["kb_consulted"] == ["Office hours (claims-handbook-2026-v3 §1.1)"]

    end_call(call_id, "Caller asked about opening hours.")
    assert repository().interactions[call_id].kb_consulted == (
        "Office hours (claims-handbook-2026-v3 §1.1)")


def test_every_entry_has_provenance_and_is_in_review():
    """Office hours and addresses go stale, and nothing about a stale fact
    looks broken — it retrieves perfectly and is simply wrong. This is the
    only check in the project that fails on the passage of time."""
    import sys
    sys.path.insert(0, "scripts")
    from check_kb import stale_entries

    assert stale_entries() == []
    # And it actually fires: every entry is overdue against a far-future date.
    assert len(stale_entries(today="2099-01-01")) == 11


# --- the model speaks before the tool answers; results must be able to
#     take it back ------------------------------------------------------
def test_a_failed_check_tells_the_agent_to_withdraw_a_premature_welcome():
    """Measured across ten live calls: the agent states a tool's outcome
    before the tool answers. One said "Thanks, Ana, you're all verified" a
    second before `verify_identity` returned. Nothing upstream prevents the
    sentence — the model may emit speech and a tool call in one completion —
    so every result that could contradict what was already said has to say
    how to withdraw it."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    wrong = tool(call_id, "verify_identity", date_of_birth="1965-02-27")
    instruction = wrong["instruction"].lower()

    assert wrong["verified"] is False
    assert "already told them they were verified" in instruction
    assert "stop using the name" in instruction
    # And still no hint about the real date of birth.
    assert "1965" not in instruction


def test_a_lockout_also_withdraws_a_premature_welcome(monkeypatch):
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-418-4492")
    for dob in ("1965-02-27", "1965-03-28", "1966-02-28"):
        result = tool(call_id, "verify_identity", date_of_birth=dob)

    assert result["locked"] is True
    assert "already told them they were verified" in result["instruction"].lower()


def test_claim_data_tells_the_agent_to_correct_what_it_guessed():
    """A live call narrated a claim's type, status and denial reason 1.7s
    before this tool answered."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-693-6678")
    tool(call_id, "verify_identity", date_of_birth="1982-01-09")
    instruction = tool(call_id, "get_claim_status")["instruction"].lower()

    assert "if anything you have already said about this claim differs" in instruction
    assert "what you said before reading it was a guess" in instruction


def test_the_knowledge_base_tells_the_agent_it_is_not_the_source():
    """The sharpest case: no tool ran, so no gate could refuse. A live call
    recited the office hours from training data nine seconds early."""
    instruction = tool(new_call(), "search_knowledge_base",
                       query="what are your office hours")["instruction"].lower()

    assert "already answered this from your own knowledge" in instruction
    assert "you are not the source here" in instruction


def test_an_emergency_that_ends_the_call_is_still_labelled():
    """A live emergency call did exactly the right thing — 911 guidance, then
    hang up so the caller could dial out — and landed in Salesforce with no
    outcome at all: identity was never verified, so the auth fallback had
    nothing, and `log_interaction` never ran because the call ended first.
    The safety path is the last thing that should be unlabelled."""
    call_id = new_call()
    emergency = tool(call_id, "report_emergency", description="my kitchen is on fire")
    assert emergency["emergency"] is True

    end_call(call_id, "Caller reported a fire; advised to call 911.", sentiment="negative")
    written = repository().interactions[call_id]

    assert written.outcome == "emergency_reported"
    assert written.authenticated is False


def test_a_verified_emergency_is_labelled_the_same_way():
    """The emergency outranks the auth state, not the other way round."""
    call_id = new_call()
    tool(call_id, "lookup_customer", phone_number="555-234-1187")
    tool(call_id, "verify_identity", date_of_birth="1979-11-03")
    tool(call_id, "report_emergency", description="there is a gas leak")

    end_call(call_id, "Verified caller reported a gas leak.", sentiment="negative")
    assert repository().interactions[call_id].outcome == "emergency_reported"


def test_a_model_supplied_outcome_still_wins_over_the_fallback():
    """The fallback is a default, not a conclusion: an agent that got as far
    as writing the record knows more than the session does."""
    call_id = new_call()
    tool(call_id, "report_emergency", description="my kitchen is on fire")
    tool(call_id, "log_interaction", summary="Fire; advised 911 and ended the call.",
         sentiment="negative", outcome="emergency_escalated")

    end_call(call_id, "Caller reported a fire.", sentiment="negative")
    assert repository().interactions[call_id].outcome == "emergency_escalated"

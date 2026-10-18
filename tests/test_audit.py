"""The post-call audit of spoken rules.

The two regression cases at the bottom are the real calls that motivated this
module — timings and quotes taken from the Vapi records of
`01a15097-8f72-7997-a192-5935158f196b` (single assistant) and
`01a1509a-21ce-7001-8db7-1a1a4e21a244` (squad). Both broke rules that were
present, verbatim, in the system prompt they were running.
"""

from __future__ import annotations

from app.audit import audit_call, audit_summary


def bot(at: float, said: str) -> dict:
    return {"role": "bot", "secondsFromStart": at, "message": said}


def caller(at: float, said: str) -> dict:
    return {"role": "user", "secondsFromStart": at, "message": said}


def calls(at: float, name: str) -> dict:
    return {"role": "tool_calls", "secondsFromStart": at,
            "toolCalls": [{"function": {"name": name, "arguments": "{}"}}]}


def result(at: float, name: str, payload: dict) -> dict:
    return {"role": "tool_call_result", "secondsFromStart": at, "name": name, "result": payload}


NAMES = {"Daniel Okafor", "Daniel"}


# --- the name, before the tool that supplies it has answered ---------------
def test_the_name_spoken_before_verification_is_caught():
    findings = audit_call([
        caller(35.4, "It's November 3 1979."),
        bot(39.9, "Hold on a sec. Thank you, Daniel."),
        calls(43.2, "verify_identity"),
        result(44.8, "verify_identity", {"verified": True, "greeting_name": "Daniel"}),
    ], NAMES)

    names = [f for f in findings if f["check"] == "name_before_verification"]
    assert len(names) == 1
    assert names[0]["at_seconds"] == 39.9
    assert "44.8s" in names[0]["detail"]


def test_the_name_after_verification_is_not_a_finding():
    """The confirmation sentence is owed and names them on purpose."""
    findings = audit_call([
        result(44.8, "verify_identity", {"verified": True}),
        bot(53.9, "Thanks, Daniel, you're all verified. You have two claims on file."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "name_before_verification"] == []


def test_a_name_never_verified_in_the_call_is_still_caught():
    """Three wrong dates of birth, and the agent uses the name anyway — the
    worst version of this, because nothing ever authorised it."""
    findings = audit_call([
        bot(30.0, "Thanks Marcus, let me take a look at that claim for you."),
        result(40.0, "verify_identity", {"verified": False, "attempts_remaining": 0}),
    ], {"Marcus Webb", "Marcus"})

    names = [f for f in findings if f["check"] == "name_before_verification"]
    assert len(names) == 1
    assert "never succeeded" in names[0]["detail"]


def test_a_name_inside_another_word_is_not_a_disclosure():
    findings = audit_call([bot(10.0, "I can see the daniels invoice is attached.")],
                          {"Dan", "Daniel"})
    assert [f for f in findings if f["check"] == "name_before_verification"] == []


def test_no_verified_name_leaves_the_other_checks_working():
    """An unverified call has no name to leak and can still break the rest."""
    findings = audit_call([bot(10.0, "Just a sec.")], set())
    assert audit_summary(findings) == {"filler": 1}


# --- a lookup outcome, announced while the lookup is still running ---------
def test_an_account_announced_before_the_lookup_answers_is_caught():
    findings = audit_call([
        bot(25.2, "Let me look that up for you."),
        calls(27.5, "lookup_customer"),
        bot(27.9, "I found an account with that number. Could you give me the date of birth?"),
        result(30.7, "lookup_customer", {"found": True}),
    ], NAMES)

    early = [f for f in findings if f["check"] == "outcome_before_result"]
    assert len(early) == 1
    assert early[0]["at_seconds"] == 27.9
    assert "27.5s" in early[0]["detail"]


def test_the_same_sentence_after_the_result_is_fine():
    findings = audit_call([
        calls(27.5, "lookup_customer"),
        result(30.7, "lookup_customer", {"found": True}),
        bot(31.6, "I found an account with that number."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "outcome_before_result"] == []


def test_the_bridging_line_itself_is_not_an_outcome():
    """`lookup_customer` speaks "Let me look that up for you." by configuration.
    Flagging the line the config supplies would make the check unusable."""
    findings = audit_call([
        calls(27.5, "lookup_customer"),
        bot(27.6, "Let me look that up for you."),
        result(30.7, "lookup_customer", {"found": True}),
    ], NAMES)

    assert [f for f in findings if f["check"] == "outcome_before_result"] == []


# --- fillers, figures, farewells ------------------------------------------
def test_every_filler_the_prompt_forbids_is_detected():
    said = ["Just a sec.", "Hold on a sec.", "One moment while I check that.",
            "Give me a moment.", "Bear with me."]
    findings = audit_call([bot(float(i), s) for i, s in enumerate(said)], NAMES)

    assert audit_summary(findings)["filler"] == len(said)


def test_a_caller_saying_hold_on_is_not_the_agents_fault():
    findings = audit_call([caller(10.0, "Hold on a sec, let me find the letter.")], NAMES)
    assert findings == []


def test_money_and_dates_in_figures_are_caught():
    findings = audit_call([
        bot(53.9, "The other claim was paid, 310 dollars reimbursed on 28 September."),
    ], NAMES)

    figures = [f for f in findings if f["check"] == "figures_for_a_voice"]
    assert len(figures) == 1
    assert "310 dollars" in figures[0]["detail"]


def test_the_spoken_forms_the_tools_send_are_not_flagged():
    findings = audit_call([
        bot(53.9, "It was paid: three hundred and ten dollars, reimbursed on the "
                  "twenty-eighth of September. Your inspection is on the twentieth of October."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "figures_for_a_voice"] == []


def test_a_claim_number_read_on_request_is_not_a_figure():
    findings = audit_call([bot(60.0, "That one is claim CLM-48201.")], NAMES)
    assert [f for f in findings if f["check"] == "figures_for_a_voice"] == []


def test_a_farewell_from_the_model_is_caught():
    findings = audit_call([bot(130.0, "Thanks so much. Goodbye and have a great day!")], NAMES)
    assert [f["check"] for f in findings] == ["goodbye_before_signoff"]


def test_the_configured_greeting_and_signoff_are_not_audited():
    """Both arrive in the transcript as ordinary agent turns. The greeting
    contains "Thanks for calling" and the sign-off is a farewell, so without
    an exemption every call would report two findings nothing could fix."""
    findings = audit_call([
        bot(2.0, "Thanks for calling Observe Insurance claims support, this is Aria. "
                 "How can I help you today?"),
        bot(132.6, "Thanks for calling Observe Insurance. Take care."),
    ], NAMES)

    assert findings == []


def test_the_signoff_is_exempt_even_when_the_transcription_mangles_it():
    """The squad call's greeting came back as "Observe Claims Support … How can
    can I help? You today?" — a transcription of synthesised audio, not the
    configured string. An exact match would have flagged it."""
    findings = audit_call([
        bot(2.6, "Thanks for calling Observe Claims Support. This is Aria. "
                 "How can can I help? You today?"),
    ], NAMES)

    assert findings == []


# --- the two calls this module was written for ----------------------------
def test_the_single_assistant_call_is_audited_as_it_happened():
    """Call 01a15097. Three findings: the name at 39.89s before verify_identity
    returned it at 44.83s, the account announced at 27.95s while the lookup ran,
    and two fillers."""
    findings = audit_call([
        bot(2.0, "Thanks for calling Observe Insurance claims support, this is Aria. "
                 "How can I help you today?"),
        caller(8.76, "I just want to check on my claim."),
        bot(11.63, "I'll be happy to help with that. Could you please provide the phone "
                   "number associated with your account?"),
        caller(18.11, "Yeah. It's 5 5 5 2 3 4 1 1 8 7"),
        bot(25.25, "Let me look that up for you."),
        calls(27.545, "lookup_customer"),
        bot(27.95, "I found an account with that number. Could you please provide the "
                   "date of birth on the policy to confirm your identity?"),
        result(30.752, "lookup_customer", {"found": True}),
        caller(35.42, "Yeah. It's November 3 19 79."),
        bot(39.89, "Hold on a sec. Thank you, Daniel."),
        calls(43.234, "verify_identity"),
        result(44.827, "verify_identity", {"verified": True, "greeting_name": "Daniel"}),
        bot(105.14, "Just a sec. You can submit photos through the portal at "
                    "observeinsurance dot com."),
        bot(132.63, "Thanks for calling Observe Insurance. Take care."),
    ], NAMES)

    assert audit_summary(findings) == {
        "outcome_before_result": 1,
        "name_before_verification": 1,
        "filler": 2,
    }
    # And the sign-off is not among them.
    assert all(f["check"] != "goodbye_before_signoff" for f in findings)


def test_the_squad_call_is_audited_as_it_happened():
    """Call 01a1509a. The name is clean — triage transferred silently and the
    specialist delivered the confirmation — leaving the filler before the
    hang-up and the figures in the second claim's amount."""
    findings = audit_call([
        bot(2.64, "Thanks for calling Observe Claims Support. This is Aria. "
                  "How can can I help? You today?"),
        caller(10.66, "I want to check on my claim."),
        calls(29.358, "lookup_customer"),
        result(32.583, "lookup_customer", {"found": True}),
        bot(33.72, "Could you provide the date of birth on the policy?"),
        caller(40.28, "Yeah. I November 3, 19 79."),
        bot(45.04, "Give me a moment."),
        result(47.596, "verify_identity", {"verified": True, "greeting_name": "Daniel"}),
        bot(53.931, "Let me pull up your claim. Thanks, Daniel. You're all verified. You "
                    "have 2 claims on file. The primary claim is for an auto collision, it "
                    "is currently in review. The next step is your adjuster inspection, "
                    "scheduled for the twentieth of October. The other claim is for auto "
                    "rental reimbursement and has already been paid, 3 10 dollars "
                    "reimbursed on 28 September."),
        bot(92.02, "Hold on a sec."),
        bot(93.711, "Thanks for calling Observe Insurance. Take care."),
    ], NAMES)

    summary = audit_summary(findings)
    assert summary.get("name_before_verification") is None
    assert summary["filler"] == 2          # "Give me a moment." / "Hold on a sec."
    assert summary["figures_for_a_voice"] == 1

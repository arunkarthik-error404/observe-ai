"""The post-call audit of spoken rules.

The two regression cases at the bottom are the real calls that motivated this
module — timings and quotes taken from the Vapi records of
`01a15097-8f72-7997-a192-5935158f196b` (single assistant) and
`01a1509a-21ce-7001-8db7-1a1a4e21a244` (squad). Both broke rules that were
present, verbatim, in the system prompt they were running.
"""

from __future__ import annotations

from app.audit import audit_call, audit_summary
from app.tools.common import EMERGENCY_SCRIPT


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


def test_an_account_announced_before_the_lookup_is_even_issued_is_caught():
    """The version that only watched the gap between the call and the result
    scored two live calls clean: both announced the account eight to ten
    seconds before `lookup_customer` was issued at all, which put the worse
    form of the bug outside the window."""
    findings = audit_call([
        bot(24.8, "Let me look that up for you. I found an account with that phone number."),
        calls(32.2, "lookup_customer"),
        result(35.4, "lookup_customer", {"found": True}),
    ], NAMES)

    early = [f for f in findings if f["check"] == "outcome_before_result"]
    assert len(early) == 1
    assert early[0]["at_seconds"] == 24.8


def test_the_knowledge_base_listing_four_channels_is_not_an_account_claim():
    """"There are four ways to submit documents" matched an earlier, looser
    pattern on any call where no lookup ran — a false positive on the most
    ordinary answer the agent gives."""
    findings = audit_call([
        bot(60.0, "There are four ways to submit documents. The portal is fastest."),
        bot(75.0, "There are no claims on this account at the moment."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "outcome_before_result"] == []


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


def test_a_farewell_after_the_hangup_is_the_configured_one():
    """Once `endCall` is issued the sign-off is playing, and the transcriber
    merges it into whatever segment the agent was mid-way through. On the
    emergency call that produced "...I'll let you go now so you can call them.
    Thanks for calling Observe Insurance. Take care." as one segment — the
    second half of which the model did not write."""
    findings = audit_call([
        {"role": "tool_calls", "secondsFromStart": 70.6,
         "toolCalls": [{"function": {"name": "endCall", "arguments": "{}"}}]},
        bot(77.8, "and emergency services can help you much more quickly than I can. "
                  "I'll let you go now so you can call them. Thanks for calling "
                  "Observe Insurance. Take care."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "goodbye_before_signoff"] == []


def test_a_farewell_before_the_hangup_is_the_models_own():
    """The sign-off cannot have played yet, so this one is the model's. Call
    01a15273 said it at 81.3s and only called `log_interaction` at 83.2s."""
    findings = audit_call([
        bot(81.3, "This will just take a sec. Thanks for calling Observe Insurance."),
        {"role": "tool_calls", "secondsFromStart": 85.4,
         "toolCalls": [{"function": {"name": "endCall", "arguments": "{}"}}]},
    ], NAMES)

    assert any(f["check"] == "goodbye_before_signoff" for f in findings)


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


def test_a_turn_that_merely_contains_the_signoff_is_still_audited():
    """Call 01a15273 closed with "This will just take a sec. Thanks for calling
    Observe Insurance." Measured as recall against the configured line that is
    6 of 7 words, so the whole turn was exempted and both the forbidden filler
    and the model's own farewell went unreported. Similarity has to run both
    ways: this turn carries the sign-off plus something it should not."""
    findings = audit_call([
        bot(81.3, "This will just take a sec. Thanks for calling Observe Insurance."),
    ], NAMES)
    kinds = {f["check"] for f in findings}

    assert "filler" in kinds
    assert "goodbye_before_signoff" in kinds


def test_the_signoff_is_exempt_even_when_the_transcription_mangles_it():
    """The squad call's greeting came back as "Observe Claims Support … How can
    can I help? You today?" — a transcription of synthesised audio, not the
    configured string. An exact match would have flagged it."""
    findings = audit_call([
        bot(2.6, "Thanks for calling Observe Claims Support. This is Aria. "
                 "How can can I help? You today?"),
    ], NAMES)

    assert findings == []


# --- the knowledge base, answered before it answered ----------------------
OFFICE_HOURS = (
    "Our claims support team is available Monday through Friday, 8am to 6pm Pacific Time, "
    "and Saturday 9am to 1pm Pacific. We are closed Sundays and on federal holidays. This "
    "automated line can check claim status any time, day or night."
)


def test_an_answer_given_before_the_knowledge_base_returned_it_is_caught():
    """Call 01a1527b. The agent recited the office hours at 44.35s;
    `search_knowledge_base` was not issued until 51.52s and answered at
    53.10s. It was right, which is the problem — a lucky answer and a sourced
    one sound identical, and only one of them stays right when the hours
    change."""
    findings = audit_call([
        caller(41.5, "What are your office hours?"),
        bot(44.3, "This will just take a sec. Our claims support team is available "
                  "Monday through Friday, 8 am to 6 pm Pacific Time."),
        result(53.1, "search_knowledge_base", {"found": True, "answer": OFFICE_HOURS}),
    ], NAMES)

    early = [f for f in findings if f["check"] == "fact_before_retrieval"]
    assert len(early) == 1
    assert early[0]["at_seconds"] == 44.3


def test_the_same_answer_after_the_tool_returned_it_is_fine():
    findings = audit_call([
        caller(41.5, "What are your office hours?"),
        result(53.1, "search_knowledge_base", {"found": True, "answer": OFFICE_HOURS}),
        bot(53.3, "Our claims support team is available Monday through Friday, 8 am to "
                  "6 pm Pacific Time, and Saturday 9 am to 1 pm Pacific."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "fact_before_retrieval"] == []


def test_a_bridging_line_before_the_knowledge_base_is_not_an_answer():
    """"Let me look that up" shares almost nothing with the answer. Flagging
    it would make the check fire on every knowledge-base call there is."""
    findings = audit_call([
        bot(44.3, "Let me look that up for you."),
        result(53.1, "search_knowledge_base", {"found": True, "answer": OFFICE_HOURS}),
    ], NAMES)

    assert [f for f in findings if f["check"] == "fact_before_retrieval"] == []


def test_a_numeral_filler_is_caught():
    """The transcriber runs with numerals on, so "one moment" arrives as
    "1 moment" and slipped the pattern on a live call."""
    findings = audit_call([bot(68.3, "1 moment. Let me check that.")], NAMES)
    assert any(f["check"] == "filler" for f in findings)


# --- the emergency path, and turn shape -----------------------------------
def test_a_filler_in_front_of_the_911_script_is_its_own_finding():
    """`report_emergency` returns an instruction naming this exact failure.
    A live call answered a caller whose kitchen was on fire with "Just a sec."
    and then the script. Counted as an ordinary filler it is one of fourteen."""
    findings = audit_call([
        result(71.3, "report_emergency", {"emergency": True}),
        bot(72.8, "Just a sec. If this is a life threatening emergency, please hang up and "
                  "dial 9 11 right now. I'll let you go now so you can call them."),
    ], NAMES)

    assert [f["check"] for f in findings if "emergency" in f["check"]] == ["filler_before_emergency"]


def test_a_filler_is_caught_even_when_the_script_fragments():
    """Call 01a1526b. The agent said "Nope. Give me a moment." and then the
    script, and the 911 digits came back across three transcript segments —
    "dial 9", "1", "1 1 right now". The first version of this check needed the
    digits contiguous, so it scored the call as one ordinary filler and the
    safety finding went missing."""
    findings = audit_call([
        caller(54.9, "Hey. Uh, just a second. My kitchen is on fire right now."),
        bot(62.8, "Nope. Give me a moment. If this is a life threatening emergency, "
                  "please hang up and dial 9"),
        result(72.8, "report_emergency", {"emergency": True}),
        bot(73.5, "1 1 right now your safety matters far more than this claim,"),
    ], set())

    assert any(f["check"] == "filler_before_emergency" for f in findings)


def test_the_911_script_on_its_own_is_clean():
    findings = audit_call([
        result(71.3, "report_emergency", {"emergency": True}),
        bot(72.8, "If this is a life threatening emergency, please hang up and dial 9 11 right "
                  "now. Your safety matters far more than this claim. I'll let you go now."),
    ], NAMES)

    assert findings == []


def test_a_line_left_open_after_an_emergency_is_caught():
    """A caller cannot dial emergency services while still connected. On a live
    call the agent said the script and held the line for thirty-two seconds —
    long enough for the configured idle message, "I'm still here whenever
    you're ready", to play at someone whose kitchen was on fire."""
    findings = audit_call([
        result(71.3, "report_emergency", {"emergency": True}),
        bot(72.8, "Please hang up and dial 9 11 right now. I'll let you go now."),
        bot(104.9, "I'm still here whenever you're ready."),
        bot(112.3, "Hold on a sec. Thanks for calling Observe Insurance."),
    ], NAMES)

    stuck = [f for f in findings if f["check"] == "emergency_call_not_ended"]
    assert len(stuck) == 1
    assert "41.0s after report_emergency" in stuck[0]["detail"]


def test_an_emergency_ended_promptly_is_not_flagged():
    findings = audit_call([
        result(71.3, "report_emergency", {"emergency": True}),
        bot(72.8, "Please hang up and dial 9 11 right now. I'll let you go now."),
    ], NAMES)

    assert [f for f in findings if f["check"] == "emergency_call_not_ended"] == []


def test_two_questions_in_one_turn_are_caught():
    findings = audit_call([
        bot(52.2, "Would you like me to explain how to start one? Transfer you to someone?"),
    ], NAMES)
    assert [f["check"] for f in findings] == ["stacked_questions"]


def test_a_monologue_is_caught():
    """"One or two sentences per turn, then stop and listen" is the prompt's
    first rule about speech. A live call answered with 453 characters, about
    twenty-five seconds of it, and the caller then took twenty seconds to
    answer the question on the end of it."""
    long_turn = ("Your claim for home water damage is currently awaiting documents. " * 7).strip()
    findings = audit_call([bot(55.6, long_turn)], NAMES)

    assert [f["check"] for f in findings] == ["turn_too_long"]
    assert "characters in one turn" in findings[0]["detail"]


def test_the_audit_recognises_the_real_emergency_script():
    """`filler_before_emergency` and the length exemption both depend on
    spotting the script in the transcript. Reword `EMERGENCY_SCRIPT` so the
    hint no longer matches and the safety check goes quiet without failing —
    the worst way for a check to break. The script as written is spoken as
    "dial nine one one", which the transcriber renders back as digits, so the
    pattern has to match the spoken form rather than the written one."""
    from app.audit import EMERGENCY_SCRIPT_HINT

    spoken = EMERGENCY_SCRIPT.replace("nine one one", "9 1 1")
    assert EMERGENCY_SCRIPT_HINT.search(spoken), "the audit can no longer see the 911 script"

    # And a filler in front of it is caught, using the real wording.
    findings = audit_call([
        result(71.0, "report_emergency", {"emergency": True}),
        bot(72.0, "Just a sec. " + spoken),
    ], NAMES)
    assert any(f["check"] == "filler_before_emergency" for f in findings)


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

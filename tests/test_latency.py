"""Turn latency, computed from Vapi's transcript timestamps.

The fixture shape is copied from a real `end-of-call-report`: `time` and
`endTime` are epoch milliseconds, `secondsFromStart` is a float, and the roles
are `user`, `bot`, `tool_calls`, `tool_call_result`.
"""

from __future__ import annotations

from app.api import payloads

BASE = 1_792_319_277_000


def msg(role: str, start_ms: int, end_ms: int | None = None) -> dict:
    return {
        "role": role,
        "time": BASE + start_ms,
        "endTime": BASE + end_ms if end_ms is not None else None,
        "secondsFromStart": start_ms / 1000,
        "duration": (end_ms - start_ms) if end_ms is not None else None,
    }


def report(messages: list[dict]) -> dict:
    return {"type": "end-of-call-report", "artifact": {"messages": messages}}


def test_latency_is_the_gap_between_caller_stopping_and_agent_starting():
    payload = report([
        msg("system", 0),
        msg("bot", 3_740, 8_380),        # greeting, nobody waiting on it
        msg("user", 11_620, 17_060),
        msg("bot", 19_010, 23_010),      # 1950ms after the caller stopped
    ])
    turns = payloads.response_latencies(payload)
    assert len(turns) == 1, "the opening greeting answers nobody, so it is not a turn"
    assert turns[0]["latency_ms"] == 1950
    assert turns[0]["tool"] is False


def test_a_turn_that_ran_a_tool_is_marked_as_one():
    """Attribution is the point: a slow turn is either ours or the voice
    stack's, and guessing wastes an afternoon."""
    payload = report([
        msg("user", 10_000, 15_000),
        msg("tool_calls", 15_400),
        msg("tool_call_result", 15_700),
        msg("bot", 16_200, 18_000),
    ])
    turns = payloads.response_latencies(payload)
    assert turns[0]["tool"] is True
    assert turns[0]["latency_ms"] == 1200


def test_the_summary_counts_tool_turns_but_will_not_compare_three_of_them():
    """The split is always counted; the medians are only published once both
    sides have enough turns to have a median worth subtracting."""
    payload = report([
        msg("user", 0, 1_000), msg("bot", 2_000, 3_000),                       # 1000
        msg("user", 4_000, 5_000), msg("tool_calls", 5_200), msg("bot", 7_000, 8_000),  # 2000
        msg("user", 9_000, 10_000), msg("bot", 11_500, 12_000),                # 1500
    ])
    summary = payloads.latency_summary(payload)
    assert summary["turns"] == 3
    assert summary["max_ms"] == 2000
    assert summary["tool_turns"] == 1 and summary["plain_turns"] == 2
    assert summary["with_tool_ms"] is None and summary["without_tool_ms"] is None


def test_a_call_with_no_caller_turns_reports_nothing():
    """Better an absent metric than a fabricated one."""
    assert payloads.latency_summary(report([msg("bot", 0, 2_000)])) == {}
    assert payloads.latency_summary({"type": "end-of-call-report"}) == {}


def test_malformed_timestamps_do_not_crash_the_webhook():
    payload = report([
        {"role": "user", "endTime": None},
        {"role": "bot", "time": "not-a-number"},
        msg("user", 0, 1_000), msg("bot", 1_500, 2_000),
    ])
    assert payloads.latency_summary(payload)["turns"] == 1


# --- reading the numbers against the floor they sit on ---------------------
def test_the_endpointing_floor_is_read_from_the_agent_config():
    """p50 of 2220ms reads as a slow agent until you know 2200ms of it is a
    pause this project deliberately configured. The floor is derived rather
    than written down twice, so retuning the wait cannot leave it stale."""
    import json

    from app.api.payloads import endpointing_floor_ms

    plan = json.load(open("vapi/agents.json", encoding="utf-8"))["defaults"]["startSpeakingPlan"]
    expected = round((plan["waitSeconds"]
                      + plan["transcriptionEndpointingPlan"]["onNoPunctuationSeconds"]) * 1000)
    assert endpointing_floor_ms() == expected
    assert expected > 0


def _summary(with_tool: int, without: int) -> dict:
    """A report with a chosen mix of turns, each a clean 2000ms gap."""
    messages, at = [], 0
    for i in range(with_tool + without):
        messages.append(msg("user", at, at + 1_000))
        if i < with_tool:
            messages.append(msg("tool_calls", at + 1_200))
        messages.append(msg("bot", at + 3_000, at + 4_000))
        at += 6_000
    return payloads.latency_summary(report(messages))


def test_a_tool_comparison_is_withheld_until_it_means_something():
    """Two tool turns against seven produced a -1260ms "tool overhead" on a
    live call — sampling noise, displayed in amber as though slower were the
    worry. A number on a dashboard gets quoted, so it is withheld instead."""
    from app.api.payloads import MIN_TURNS_TO_COMPARE

    thin = _summary(with_tool=2, without=7)
    assert thin["with_tool_ms"] is None
    assert thin["without_tool_ms"] is None
    assert thin["tool_turns"] == 2 and thin["plain_turns"] == 7

    enough = _summary(with_tool=MIN_TURNS_TO_COMPARE, without=MIN_TURNS_TO_COMPARE)
    assert enough["with_tool_ms"] is not None
    assert enough["without_tool_ms"] is not None


def test_the_summary_carries_the_floor_so_the_console_need_not_guess():
    from app.api.payloads import endpointing_floor_ms

    assert _summary(4, 4)["floor_ms"] == endpointing_floor_ms()

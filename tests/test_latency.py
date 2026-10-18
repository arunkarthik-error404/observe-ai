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


def test_the_summary_separates_tool_turns_from_the_rest():
    payload = report([
        msg("user", 0, 1_000), msg("bot", 2_000, 3_000),                       # 1000
        msg("user", 4_000, 5_000), msg("tool_calls", 5_200), msg("bot", 7_000, 8_000),  # 2000
        msg("user", 9_000, 10_000), msg("bot", 11_500, 12_000),                # 1500
    ])
    summary = payloads.latency_summary(payload)
    assert summary["turns"] == 3
    assert summary["max_ms"] == 2000
    assert summary["with_tool_ms"] == 2000
    assert summary["without_tool_ms"] in (1000, 1500)


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

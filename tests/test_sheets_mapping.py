"""Tests for the Sheets adapter itself, against a fake worksheet.

These exist because of a bug the in-memory repository could never catch: it
dedupes interactions by dict key, so `test_duplicate_end_of_call_report_is_
idempotent` passed while the real Sheets write appended a second row. The cause
was header canonicalisation — `_canon` strips underscores, so looking for the
literal "call_id" in canonicalised headers never matched and the whole
idempotency check was skipped. Everything below pins that seam.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models import InteractionRecord
from app.repositories.sheets import (
    CLAIM_ALIASES,
    CUSTOMER_ALIASES,
    SheetsRepository,
    _map_row,
    describe_error,
)
from app.seed import INTERACTION_HEADERS


class FakeWorksheet:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = [list(r) for r in rows]
        self.appended: list[list[str]] = []
        self.updated: list[tuple[int, list[str]]] = []

    def row_values(self, index: int) -> list[str]:
        return list(self.rows[index - 1]) if len(self.rows) >= index else []

    def col_values(self, index: int) -> list[str]:
        return [row[index - 1] if len(row) >= index else "" for row in self.rows]

    def append_row(self, values: list[str], **_: Any) -> None:
        self.rows.append(list(values))
        self.appended.append(list(values))

    def update(self, values: list[list[str]], range_name: str, **_: Any) -> None:
        row_number = int(range_name.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        self.rows[row_number - 1] = list(values[0])
        self.updated.append((row_number, list(values[0])))

    def get_all_records(self) -> list[dict[str, Any]]:
        header = self.rows[0]
        return [dict(zip(header, row, strict=False)) for row in self.rows[1:]]


class FakeBook:
    title = "fake"

    def __init__(self, tabs: dict[str, FakeWorksheet]) -> None:
        self.tabs = tabs

    def worksheet(self, name: str) -> FakeWorksheet:
        if name not in self.tabs:
            raise KeyError(name)
        return self.tabs[name]


@pytest.fixture
def repo_and_tab(monkeypatch):
    tab = FakeWorksheet([list(INTERACTION_HEADERS)])
    repo = SheetsRepository()
    monkeypatch.setattr(repo, "_sheet", lambda: FakeBook({"Interactions": tab}))
    return repo, tab


def test_interaction_write_is_idempotent_on_call_id(repo_and_tab):
    repo, tab = repo_and_tab
    record = InteractionRecord(call_id="call_abc", caller_name="Daniel Okafor", summary="first")

    repo.write_interaction(record)
    repo.write_interaction(record.model_copy(update={"summary": "redelivery"}))

    assert len(tab.appended) == 1, "a redelivered report must not append a second row"
    assert len(tab.rows) == 2, "header plus exactly one data row"


def test_the_two_write_paths_merge_into_one_row(repo_and_tab):
    """In-call tool first, then the richer end-of-call report on top."""
    repo, tab = repo_and_tab

    repo.write_interaction(InteractionRecord(
        call_id="call_merge",
        caller_name="Daniel Okafor",
        summary="Agent's own mid-call summary.",
        sentiment="positive",
        outcome="status_provided",
        source="in-call-tool",
    ))
    repo.write_interaction(InteractionRecord(
        call_id="call_merge",
        caller_name="Daniel Okafor",
        summary="Post-call analysis summary.",
        sentiment="neutral",
        outcome="",                      # webhook had nothing better
        duration_seconds=95.4,
        recording_url="https://example.com/rec.wav",
        source="end-of-call-report",
    ))

    assert len(tab.rows) == 2, "one call must be one row"
    row = dict(zip(INTERACTION_HEADERS, tab.rows[1], strict=False))
    assert row["summary"] == "Post-call analysis summary."   # newer wins
    assert row["duration_seconds"] == "95.4"                  # webhook fills the gap
    assert row["recording_url"].endswith("rec.wav")
    assert row["outcome"] == "status_provided"                # empty never overwrites
    assert len(tab.updated) == 1


def test_a_different_call_still_appends(repo_and_tab):
    repo, tab = repo_and_tab
    repo.write_interaction(InteractionRecord(call_id="call_one", caller_name="A"))
    repo.write_interaction(InteractionRecord(call_id="call_two", caller_name="B"))
    assert len(tab.rows) == 3
    assert len(tab.updated) == 0


def test_interaction_row_lands_in_header_order_even_when_reordered(repo_and_tab, monkeypatch):
    repo, _ = repo_and_tab
    # A business user reorders and renames columns in the sheet.
    tab = FakeWorksheet([["Call ID", "Caller Name", "sentiment", "timestamp"]])
    monkeypatch.setattr(repo, "_sheet", lambda: FakeBook({"Interactions": tab}))

    repo.write_interaction(
        InteractionRecord(call_id="call_xyz", caller_name="Priya Raman", sentiment="positive")
    )

    row = tab.appended[0]
    assert row[0] == "call_xyz"
    assert row[1] == "Priya Raman"
    assert row[2] == "positive"
    assert row[3].startswith("20")


def test_booleans_are_written_as_words_not_python_literals(repo_and_tab):
    repo, tab = repo_and_tab
    repo.write_interaction(
        InteractionRecord(call_id="c1", authenticated=True, escalated=False)
    )
    row = dict(zip(INTERACTION_HEADERS, tab.appended[0], strict=False))
    assert row["authenticated"] == "yes"
    assert row["escalated"] == "no"


@pytest.mark.parametrize(
    "header",
    ["phone", "Phone", "Phone Number", "phone_number", "PHONENUMBER", "Mobile"],
)
def test_customer_phone_header_aliases(header):
    assert _map_row({header: "555-234-1187"}, CUSTOMER_ALIASES) == {"phone": "555-234-1187"}


def test_claim_headers_map_and_unknown_columns_are_ignored():
    mapped = _map_row(
        {
            "Claim Number": "CLM-48255",
            "Status": "Documents Required",
            "Required Docs": "photos; invoice",
            "Due Date": "2026-10-29",
            "Internal Notes For Ops": "ignore me",
        },
        CLAIM_ALIASES,
    )
    assert mapped["claim_id"] == "CLM-48255"
    assert mapped["status"] == "Documents Required"
    assert mapped["required_documents"] == "photos; invoice"
    assert mapped["submission_deadline"] == "2026-10-29"
    assert "ignore me" not in mapped.values()


def test_describe_error_surfaces_an_empty_permission_error():
    """gspread 6 raises a bare PermissionError; str(exc) is ''."""

    class FakeResponse:
        status_code = 403
        text = '{"error":{"message":"The caller does not have permission"}}'

    class FakeAPIError(Exception):
        response = FakeResponse()

    try:
        try:
            raise FakeAPIError("APIError: [403]: The caller does not have permission")
        except FakeAPIError as cause:
            raise PermissionError from cause
    except PermissionError as exc:
        described = describe_error(exc)

    assert "PermissionError" in described
    assert "does not have permission" in described
    assert "http 403" in described


def test_config_errors_are_not_retried():
    class FakeResponse:
        status_code = 403

    class FakeAPIError(Exception):
        response = FakeResponse()

    assert SheetsRepository._is_transient(FakeAPIError()) is False

    class RateLimited(Exception):
        class response:  # noqa: N801
            status_code = 429

    assert SheetsRepository._is_transient(RateLimited()) is True

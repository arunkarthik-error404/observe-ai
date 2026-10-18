"""Google Sheets implementation — Integration #1 (read) and #2 (write).

Auth: a Google Cloud **service account**. The JSON key is held as an env var
(never in the repo), the sheet is shared with the service account's email as
an editor, and the OAuth scope is narrowed to Sheets + Drive file access. No
end-user OAuth consent screen, no refresh-token dance — the agent is a backend
daemon, so two-legged auth is the right shape.

Data mapping: column *headers* define the contract, not column positions, so a
business user can reorder or insert columns in the sheet without breaking the
agent. Headers are matched case/space/underscore-insensitively against the
aliases below, and anything unrecognised is ignored.

Reliability: reads are cached briefly (a caller usually triggers 2-3 lookups
in one breath) and every call is wrapped in bounded exponential-backoff retry,
because Sheets returns 429s under even modest concurrency. Writes are
idempotent on call_id so a Vapi webhook redelivery cannot double-log a call.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..models import Claim, Customer, InteractionRecord, normalize_phone
from ..seed import INTERACTION_HEADERS
from .base import ClaimsRepository, RepositoryError

log = logging.getLogger("claims.sheets")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

CUSTOMER_ALIASES: dict[str, str] = {
    "phone": "phone", "phonenumber": "phone", "phone_number": "phone", "mobile": "phone",
    "fullname": "full_name", "full_name": "full_name", "name": "full_name", "customername": "full_name",
    "preferredname": "preferred_name", "preferred_name": "preferred_name", "firstname": "preferred_name",
    "dateofbirth": "date_of_birth", "date_of_birth": "date_of_birth", "dob": "date_of_birth",
    "policyid": "policy_id", "policy_id": "policy_id", "policynumber": "policy_id", "policy": "policy_id",
    "email": "email", "emailaddress": "email",
}

CLAIM_ALIASES: dict[str, str] = {
    "claimid": "claim_id", "claim_id": "claim_id", "claimnumber": "claim_id", "claim": "claim_id",
    "phone": "phone", "phonenumber": "phone", "phone_number": "phone", "customerphone": "phone",
    "claimtype": "claim_type", "claim_type": "claim_type", "type": "claim_type",
    "status": "status", "claimstatus": "status",
    "statusdetail": "status_detail", "status_detail": "status_detail", "detail": "status_detail",
    "notes": "status_detail",
    "amount": "amount", "approvedamount": "amount", "payout": "amount",
    "adjuster": "adjuster_name", "adjustername": "adjuster_name", "adjuster_name": "adjuster_name",
    "requireddocuments": "required_documents", "required_documents": "required_documents",
    "requireddocs": "required_documents", "documents": "required_documents",
    "submissiondeadline": "submission_deadline", "submission_deadline": "submission_deadline",
    "deadline": "submission_deadline", "duedate": "submission_deadline",
    "lastupdated": "last_updated", "last_updated": "last_updated", "updated": "last_updated",
    "nextstep": "next_step", "next_step": "next_step", "nextsteps": "next_step",
}


def describe_error(exc: BaseException) -> str:
    """Flatten a Google/gspread exception into something a human can act on.

    gspread 6 converts an API 403 into a bare `PermissionError()` whose str() is
    empty, so the actionable detail ("The caller does not have permission")
    only exists on `__cause__` and its HTTP response. Logging str(exc) alone
    turns a one-line fix into a debugging session.
    """
    parts = [type(exc).__name__]
    text = str(exc).strip()
    if text:
        parts.append(text)

    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        cause_text = str(cause).strip()
        parts.append(f"caused by {type(cause).__name__}: {cause_text}" if cause_text
                     else f"caused by {type(cause).__name__}")

    response = getattr(exc, "response", None) or getattr(cause, "response", None)
    if response is not None:
        status = getattr(response, "status_code", "?")
        body = getattr(response, "text", "")[:300]
        parts.append(f"http {status}: {body}")
    return " | ".join(parts)


def _canon(header: str) -> str:
    return "".join(ch for ch in str(header).lower() if ch.isalnum())


def _map_row(row: dict[str, Any], aliases: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key, value in row.items():
        field = aliases.get(_canon(key))
        if field:
            mapped[field] = str(value).strip()
    return mapped


class SheetsRepository(ClaimsRepository):
    CACHE_TTL = 20.0  # seconds

    def __init__(self) -> None:
        self._lock = threading.Lock()          # guards the lazy client
        self._write_lock = threading.Lock()    # serialises read-modify-write on Interactions
        self._spreadsheet = None
        self._cache: dict[str, tuple[float, Any]] = {}

    # --- connection -------------------------------------------------------
    def _credentials(self):
        from google.oauth2.service_account import Credentials

        cfg = settings()
        if cfg.google_credentials_json.strip():
            info = json.loads(cfg.google_credentials_json)
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        if cfg.google_credentials_file.strip():
            # Resolved against the project root, not the process working
            # directory, so the path works from scripts/ and from uvicorn alike.
            path = Path(cfg.google_credentials_file.strip())
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            return Credentials.from_service_account_file(str(path), scopes=SCOPES)
        raise RepositoryError("No Google service-account credentials configured")

    def _sheet(self):
        with self._lock:
            if self._spreadsheet is None:
                import gspread

                cfg = settings()
                if not cfg.sheet_id:
                    raise RepositoryError("SHEET_ID is not set")
                client = gspread.authorize(self._credentials())
                try:  # gspread >= 6 exposes a global request timeout
                    client.set_timeout(cfg.sheets_timeout_seconds)
                except Exception:  # pragma: no cover - older gspread
                    pass
                self._spreadsheet = client.open_by_key(cfg.sheet_id)
            return self._spreadsheet

    # --- resilience -------------------------------------------------------
    def _with_retry(self, label: str, fn):
        cfg = settings()
        last: Exception | None = None
        for attempt in range(1, cfg.sheets_max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - we classify below
                last = exc
                transient = self._is_transient(exc)
                log.warning(
                    "sheets_call_failed",
                    extra={"label": label, "attempt": attempt, "transient": transient,
                           "error": describe_error(exc)},
                )
                if not transient or attempt == cfg.sheets_max_retries:
                    break
                # full jitter backoff: 0.4s, 0.8s, 1.6s (+/- jitter)
                time.sleep(min(2.0, 0.4 * (2 ** (attempt - 1))) * (0.5 + random.random()))
                with self._lock:  # force a fresh client on the next attempt
                    self._spreadsheet = None
        raise RepositoryError(f"{label} failed: {describe_error(last)}" if last
                              else f"{label} failed") from last

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        cause = exc.__cause__ or exc.__context__
        response = getattr(exc, "response", None) or getattr(cause, "response", None)
        status = getattr(response, "status_code", None)
        if status in {429, 500, 502, 503, 504}:
            return True
        if status in {400, 401, 403, 404}:  # config problems; retrying wastes the caller's time
            return False
        text = describe_error(exc).lower()
        return any(s in text for s in ("timeout", "timed out", "temporarily", "connection", "429", "503"))

    def _records(self, tab: str) -> list[dict[str, Any]]:
        cached = self._cache.get(tab)
        if cached and time.time() - cached[0] < self.CACHE_TTL:
            return cached[1]
        rows = self._with_retry(f"read:{tab}", lambda: self._sheet().worksheet(tab).get_all_records())
        self._cache[tab] = (time.time(), rows)
        return rows

    def invalidate(self) -> None:
        self._cache.clear()

    # --- reads (Integration #1) ------------------------------------------
    def find_customer_by_phone(self, phone: str) -> Customer | None:
        key = normalize_phone(phone)
        if not key:
            return None
        for raw in self._records(settings().customers_tab):
            row = _map_row(raw, CUSTOMER_ALIASES)
            if normalize_phone(row.get("phone")) == key:
                row["phone"] = key
                row.setdefault("full_name", "")
                return Customer(**row)
        return None

    def claims_for_phone(self, phone: str) -> list[Claim]:
        key = normalize_phone(phone)
        claims: list[Claim] = []
        for raw in self._records(settings().claims_tab):
            row = _map_row(raw, CLAIM_ALIASES)
            if normalize_phone(row.get("phone")) != key:
                continue
            row["phone"] = key
            row["claim_id"] = (row.get("claim_id") or "").strip()
            claims.append(Claim(**row))
        return claims

    # --- write (Integration #2) ------------------------------------------
    def _interactions_sheet(self):
        sheet = self._sheet()
        cfg = settings()
        try:
            ws = sheet.worksheet(cfg.interactions_tab)
        except Exception:
            ws = sheet.add_worksheet(title=cfg.interactions_tab, rows=1000, cols=len(INTERACTION_HEADERS))
            ws.append_row(INTERACTION_HEADERS, value_input_option="USER_ENTERED")
        if not ws.row_values(1):
            ws.append_row(INTERACTION_HEADERS, value_input_option="USER_ENTERED")
        return ws

    @staticmethod
    def _row_for(record: InteractionRecord, header: list[str]) -> list[str]:
        payload = record.model_dump()
        payload["authenticated"] = "yes" if record.authenticated else "no"
        payload["escalated"] = "yes" if record.escalated else "no"
        return [str(payload.get(_canon_to_field(h), "") or "") for h in header]

    def write_interaction(self, record: InteractionRecord) -> None:
        """Upsert one row per call_id.

        A call produces up to two records: the in-call `log_interaction` tool
        and the `end-of-call-report` webhook (which Vapi may also redeliver).
        Checking "does this call_id exist yet?" and then appending is a
        read-then-write race — the async in-call write and the webhook can both
        read an empty column and both append, which is exactly what happened on
        the first live run. So this merges instead: find the row for this call,
        update it in place, and only append when there is none. One call is one
        row that gets enriched as better data arrives (the webhook's duration,
        recording URL and analysis summary land on top of what the agent logged
        mid-call), and redelivery is harmless by construction.

        The lock serialises the read-modify-write within this process. Sheets
        has no compare-and-set, so a second replica could still interleave —
        one more reason it is a demo system of record and not a production one.
        """

        def _upsert():
            ws = self._interactions_sheet()
            header = ws.row_values(1) or list(INTERACTION_HEADERS)
            values = self._row_for(record, header)
            canon_headers = [_canon(h) for h in header]

            # `_canon` strips underscores, so the key has to be canonicalised
            # too — matching on a literal "call_id" never fires and silently
            # disables the whole check.
            key = _canon("call_id")
            if record.call_id and key in canon_headers:
                column = ws.col_values(canon_headers.index(key) + 1)
                if record.call_id in column[1:]:
                    row_number = column.index(record.call_id) + 1
                    current = ws.row_values(row_number)
                    merged = [
                        new or (current[i] if i < len(current) else "")
                        for i, new in enumerate(values)
                    ]
                    ws.update(values=[merged], range_name=f"A{row_number}",
                              value_input_option="USER_ENTERED")
                    log.info("interaction_merged",
                             extra={"call_id": record.call_id, "row": row_number,
                                    "source": record.source})
                    return

            ws.append_row(values, value_input_option="USER_ENTERED")
            log.info("interaction_appended",
                     extra={"call_id": record.call_id, "source": record.source})

        with self._write_lock:
            self._with_retry("write:interaction", _upsert)

    def health(self) -> dict:
        try:
            title = self._sheet().title
            return {"backend": "sheets", "ok": True, "spreadsheet": title}
        except Exception as exc:  # noqa: BLE001
            return {"backend": "sheets", "ok": False, "error": describe_error(exc)}


_INTERACTION_FIELD_BY_CANON = {_canon(h): h for h in INTERACTION_HEADERS}


def _canon_to_field(header: str) -> str:
    """Resolve a sheet header back to an InteractionRecord field name."""
    return _INTERACTION_FIELD_BY_CANON.get(_canon(header), _canon(header))

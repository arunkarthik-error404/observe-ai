"""Salesforce implementation of the data layer.

Object mapping — standard objects wherever one already means the right thing,
because an insurer's org will already have these and a demo that invents its
own objects proves less:

    Contact   the customer.  Phone, Birthdate, Name, plus Policy_Id__c and
              Normalized_Phone__c (see below).
    Case      the claim.     Status, Subject, Description, ContactId, plus a
              handful of claim-specific custom fields.
    Task      the post-call record — Salesforce's own "Log a Call". WhoId links
              it to the Contact, so the interaction shows up on the customer's
              activity timeline where a human would look for it.

Two decisions worth defending:

**Normalized_Phone__c.** SOQL has no regex and phone numbers in a real org are
typed by humans: "(555) 234-1187", "+1 555 234 1187", "5552341187". Matching on
`Phone` means either a LIKE scan or luck. A normalized, indexed external-id
field makes the lookup exact and fast — the same ten-digit normalisation the
rest of the system already applies at its boundary.

**Call_Id__c as an External Id.** Salesforce can PATCH by external id, which is
a genuine upsert: one round trip, no read-then-write race, and a redelivered
webhook updates the same Task instead of creating a second one. This is the
thing Sheets could not do natively and had to be built by hand with a lock.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from ..models import Claim, Customer, InteractionRecord, normalize_phone
from .base import ClaimsRepository, RepositoryError
from .salesforce_auth import SalesforceAuthError, tokens

log = logging.getLogger("claims.salesforce")

# Claim status comes from the Case; these are the Salesforce picklist values we
# seed. Anything unrecognised is passed through rather than mangled.
# Claim_Status__c rather than the standard Status picklist: "Documents
# Required" and "Denied" are not standard Case values, and widening a picklist
# is org-admin work that a demo should not require. The standard Status still
# carries Working/Closed so the Case behaves normally for anyone looking at it
# in Salesforce.
CASE_FIELDS = (
    "Id, CaseNumber, Status, Claim_Status__c, Subject, Description, Type, "
    "Claim_Number__c, Claim_Amount__c, Adjuster_Name__c, Required_Documents__c, "
    "Submission_Deadline__c, Next_Step__c, LastModifiedDate"
)
CONTACT_FIELDS = (
    "Id, FirstName, LastName, Name, Phone, Birthdate, Email, "
    "Policy_Id__c, Normalized_Phone__c"
)


def soql_escape(value: str) -> str:
    """Escape a value for inlining in SOQL. Callers control this input, so it
    is sanitised rather than trusted — SOQL injection is a real class of bug."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class SalesforceRepository(ClaimsRepository):
    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._contact_cache: dict[str, Customer] = {}

    # --- transport -------------------------------------------------------
    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs) -> Any:
        token, instance = tokens.credentials()
        if not instance:
            raise RepositoryError("Salesforce instance URL unknown; check the auth flow")

        if self._client is None:
            self._client = httpx.Client(timeout=settings().sf_timeout_seconds)

        url = f"{instance}{path}"
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RepositoryError(f"salesforce request failed: {exc}") from exc

        # A session can die before its clock says so. One forced refresh, then
        # give up — looping on 401 just delays the caller.
        if response.status_code == 401 and retry_auth:
            log.info("salesforce_token_expired_early")
            tokens.invalidate()
            return self._request(method, path, retry_auth=False, **kwargs)

        if response.status_code >= 300:
            raise RepositoryError(
                f"salesforce {method} {path} -> {response.status_code}: {response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _query(self, soql: str) -> list[dict[str, Any]]:
        version = settings().sf_api_version
        payload = self._request("GET", f"/services/data/{version}/query?q={quote(soql)}")
        return payload.get("records", [])

    # --- reads (Integration #1) ------------------------------------------
    def find_customer_by_phone(self, phone: str) -> Customer | None:
        key = normalize_phone(phone)
        if not key:
            return None
        if key in self._contact_cache:
            return self._contact_cache[key]

        try:
            records = self._query(
                f"SELECT {CONTACT_FIELDS} FROM Contact "
                f"WHERE Normalized_Phone__c = '{soql_escape(key)}' LIMIT 1"
            )
        except SalesforceAuthError as exc:
            raise RepositoryError(str(exc)) from exc

        if not records:
            return None

        record = records[0]
        customer = Customer(
            phone=key,
            full_name=record.get("Name") or "",
            preferred_name=record.get("FirstName") or "",
            date_of_birth=record.get("Birthdate") or "",   # already YYYY-MM-DD
            policy_id=record.get("Policy_Id__c") or "",
            email=record.get("Email") or "",
            source_id=record.get("Id") or "",
        )
        self._contact_cache[key] = customer
        return customer

    def claims_for_phone(self, phone: str) -> list[Claim]:
        customer = self.find_customer_by_phone(phone)
        if customer is None:
            return []
        contact_id = customer.source_id
        if not contact_id:
            return []

        records = self._query(
            f"SELECT {CASE_FIELDS} FROM Case "
            f"WHERE ContactId = '{soql_escape(contact_id)}' "
            f"ORDER BY CreatedDate DESC LIMIT 20"
        )
        return [self._to_claim(record, customer.phone) for record in records]

    @staticmethod
    def _to_claim(record: dict[str, Any], phone: str) -> Claim:
        deadline = record.get("Submission_Deadline__c") or ""
        updated = (record.get("LastModifiedDate") or "")[:10]
        return Claim(
            claim_id=record.get("Claim_Number__c") or record.get("CaseNumber") or "",
            phone=phone,
            claim_type=record.get("Type") or record.get("Subject") or "",
            status=record.get("Claim_Status__c") or record.get("Status") or "",
            status_detail=record.get("Description") or "",
            amount=_format_amount(record.get("Claim_Amount__c")),
            adjuster_name=record.get("Adjuster_Name__c") or "",
            required_documents=record.get("Required_Documents__c") or "",
            submission_deadline=deadline,
            last_updated=updated,
            next_step=record.get("Next_Step__c") or "",
        )

    # --- write (Integration #2) ------------------------------------------
    def write_interaction(self, record: InteractionRecord) -> None:
        """Upsert a Task by external id.

        The two write paths (in-call tool and end-of-call webhook) can land in
        the same second. When they do, both upserts resolve "no such external
        id" and both try to insert; one wins and the other gets
        DUPLICATE_VALUE. Retrying once is the whole fix — by then the record
        exists, so the second attempt matches and updates it.

        Worth contrasting with the Sheets adapter: there the same race produced
        two rows silently, and preventing it needed a lock that only works
        inside one process. Here the unique constraint *detects* the conflict,
        so an optimistic retry is correct and keeps working across replicas.
        """
        version = settings().sf_api_version
        contact_id = ""
        if record.caller_phone:
            customer = self.find_customer_by_phone(record.caller_phone)
            contact_id = customer.source_id if customer else ""

        body: dict[str, Any] = {
            "Subject": f"Claims call — {record.outcome or 'handled'}",
            "Description": _description(record),
            "Status": "Completed",
            # TaskSubtype is deliberately not sent. Salesforce accepts it on
            # insert but rejects it on update, and a blind upsert cannot know
            # which one it is doing — so the second write of a call would fail
            # on a cosmetic field. CallDurationInSeconds already makes this
            # read as a logged call.
            "ActivityDate": record.timestamp[:10] if record.timestamp else None,
            "Call_Sentiment__c": record.sentiment,
            "Call_Intent__c": record.intent[:255],
            "Identity_Verified__c": record.authenticated,
            "Escalated__c": record.escalated,
            "Claim_Numbers__c": record.claim_ids[:255],
            "Recording_Url__c": record.recording_url[:255],
        }
        if contact_id:
            body["WhoId"] = contact_id
        if record.duration_seconds is not None:
            body["CallDurationInSeconds"] = int(record.duration_seconds)
        body = {k: v for k, v in body.items() if v not in (None, "")}

        path = (f"/services/data/{version}/sobjects/Task/Call_Id__c/"
                f"{quote(record.call_id, safe='')}")
        for attempt in (1, 2):
            try:
                self._request("PATCH", path, json=body,
                              headers={"Content-Type": "application/json"})
                log.info("interaction_upserted",
                         extra={"call_id": record.call_id, "contact": contact_id,
                                "attempt": attempt})
                return
            except RepositoryError as exc:
                if attempt == 2 or "DUPLICATE_VALUE" not in str(exc):
                    raise
                log.info("interaction_upsert_raced", extra={"call_id": record.call_id})
                time.sleep(0.4)

    def record_url(self, customer: Customer) -> str:
        """Lightning deep link to the Contact. Opens on the activity timeline,
        where the Task this call writes will appear."""
        if not customer or not customer.source_id:
            return ""
        try:
            _, instance = tokens.credentials()
        except SalesforceAuthError:
            return ""
        if not instance:
            return ""
        return f"{instance}/lightning/r/Contact/{customer.source_id}/view"

    # --- diagnostics ------------------------------------------------------
    def health(self) -> dict:
        try:
            version = settings().sf_api_version
            limits = self._request("GET", f"/services/data/{version}/limits")
            daily = limits.get("DailyApiRequests", {})
            _, instance = tokens.credentials()
            return {
                "backend": "salesforce",
                "ok": True,
                "instance": instance,
                "api_calls_remaining": daily.get("Remaining"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"backend": "salesforce", "ok": False, "error": str(exc)[:300]}

    def invalidate(self) -> None:
        self._contact_cache.clear()


DESCRIPTION_LIMIT = 32_000  # Task.Description is a 32,768-char long text area


def _description(record: InteractionRecord) -> str:
    """Summary first, then the full transcript beneath it.

    Both live in the standard Description because Activity — the entity behind
    Task and Event — does not permit Long Text Area custom fields. That turns
    out better anyway: Description is on every Task layout by default, so a
    human sees the call without an admin adding a field. Summary goes first
    because that is what gets read; the transcript is there when they need the
    detail.
    """
    parts = [record.summary.strip()] if record.summary.strip() else []
    if record.callback_number:
        # Second, because it is the one line in here someone has to act on.
        # It goes in the text rather than a custom field because it is not the
        # account number and must never be what a contact is matched on — and
        # a number that exists only inside the transcript is a callback nobody
        # makes.
        parts.append(f"Call back on: {record.callback_number}")
    if record.transcript.strip():
        parts.append("--- Transcript ---\n" + record.transcript.strip())
    text = "\n\n".join(parts)
    if len(text) <= DESCRIPTION_LIMIT:
        return text
    # Truncate the transcript rather than lose the write; the summary is small
    # and must survive intact.
    return text[: DESCRIPTION_LIMIT - 20].rstrip() + "\n… [truncated]"


def _format_amount(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)

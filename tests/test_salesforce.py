"""Salesforce adapter, tested against a mock transport.

No org, no network, no credentials. What's being checked is the part that
actually breaks in an integration: the queries sent, the field mapping both
ways, token refresh on an early 401, and that a redelivered record upserts
instead of duplicating.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import config
from app.models import InteractionRecord
from app.repositories import salesforce_auth
from app.repositories.salesforce import SalesforceRepository, soql_escape

INSTANCE = "https://observe-dev.my.salesforce.com"

CONTACT = {
    "Id": "003XX000004TmiQ",
    "Name": "Ana Duarte",
    "FirstName": "Ana",
    "LastName": "Duarte",
    "Phone": "(555) 693-6678",
    "Birthdate": "1982-01-09",
    "Email": "ana.duarte@example.com",
    "Policy_Id__c": "POL-11402",
    "Normalized_Phone__c": "5556936678",
}

CASE = {
    "Id": "500XX000002Nq1R",
    "CaseNumber": "00001027",
    "Status": "Denied",
    "Subject": "Health - outpatient",
    "Type": "Health - outpatient",
    "Description": "Denied because the provider was out of network.",
    "Claim_Number__c": "CLM-48402",
    "Claim_Amount__c": None,
    "Adjuster_Name__c": "Jon Mbeki",
    "Required_Documents__c": None,
    "Submission_Deadline__c": "2026-11-11",
    "Next_Step__c": "You can appeal within 30 days of the decision letter.",
    "LastModifiedDate": "2026-10-09T11:04:00.000+0000",
}


class FakeOrg:
    """Records every request so the test can assert on what was sent."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.patches: list[dict[str, Any]] = []
        self.token_requests = 0
        self.contacts = [CONTACT]
        self.cases = [CASE]
        self.expire_next_call = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))

        if url.path.endswith("/services/oauth2/token"):
            self.token_requests += 1
            return httpx.Response(200, json={
                "access_token": f"token-{self.token_requests}",
                "instance_url": INSTANCE,
                "expires_in": 3600,
            })

        if self.expire_next_call:
            self.expire_next_call = False
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])

        if "/query" in url.path:
            soql = parse_qs(url.query)["q"][0]
            self.queries.append(soql)
            if "FROM Contact" in soql:
                matched = [c for c in self.contacts if c["Normalized_Phone__c"] in soql]
                return httpx.Response(200, json={"records": matched, "totalSize": len(matched)})
            if "FROM Case" in soql:
                matched = [c for c in self.cases if CONTACT["Id"] in soql]
                return httpx.Response(200, json={"records": matched, "totalSize": len(matched)})
            return httpx.Response(200, json={"records": []})

        if "/sobjects/Task/Call_Id__c/" in url.path:
            self.patches.append({
                "call_id": url.path.rsplit("/", 1)[-1],
                "body": json.loads(request.content or b"{}"),
            })
            return httpx.Response(200, json={"id": "00TXX0000012345", "success": True})

        if url.path.endswith("/limits"):
            return httpx.Response(200, json={"DailyApiRequests": {"Max": 15000, "Remaining": 14873}})

        return httpx.Response(404, json={"message": f"unexpected {url.path}"})


@pytest.fixture
def org(monkeypatch):
    fake = FakeOrg()
    transport = httpx.MockTransport(fake.handler)

    config.settings.cache_clear()
    monkeypatch.setenv("BACKEND", "salesforce")
    monkeypatch.setenv("SF_CLIENT_ID", "consumer-key")
    monkeypatch.setenv("SF_CLIENT_SECRET", "consumer-secret")

    # Both the token call and the data calls go through the mock.
    real_post = httpx.post
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(
        transport=transport).post(url, **kw))

    repo = SalesforceRepository()
    repo._client = httpx.Client(transport=transport)  # noqa: SLF001
    salesforce_auth.tokens.invalidate()

    yield repo, fake

    salesforce_auth.tokens.invalidate()
    monkeypatch.setattr(httpx, "post", real_post)
    config.settings.cache_clear()


# --- reads ----------------------------------------------------------------
def test_customer_lookup_queries_the_normalised_phone_field(org):
    repo, fake = org
    customer = repo.find_customer_by_phone("(555) 693-6678")

    assert customer is not None
    assert customer.full_name == "Ana Duarte"
    assert customer.date_of_birth == "1982-01-09"
    assert customer.policy_id == "POL-11402"

    soql = fake.queries[0]
    # Matching on Phone would need a LIKE scan and would miss a format variant.
    assert "Normalized_Phone__c = '5556936678'" in soql
    assert "FROM Contact" in soql


def test_an_unknown_number_returns_none_not_an_error(org):
    repo, _ = org
    assert repo.find_customer_by_phone("555-999-8888") is None


def test_claims_map_case_fields_onto_the_domain_model(org):
    repo, fake = org
    claims = repo.claims_for_phone("5556936678")

    assert len(claims) == 1
    claim = claims[0]
    assert claim.claim_id == "CLM-48402"
    assert claim.status == "Denied"
    assert claim.adjuster_name == "Jon Mbeki"
    assert claim.submission_deadline == "2026-11-11"
    assert claim.last_updated == "2026-10-09"      # trimmed from the SF timestamp
    assert claim.needs_documents is False
    assert "appeal" in claim.next_step

    assert any("FROM Case" in q and CONTACT["Id"] in q for q in fake.queries)


def test_the_contact_is_fetched_once_per_call_not_per_question(org):
    """A caller turn can trigger several tools; each must not re-query."""
    repo, fake = org
    repo.find_customer_by_phone("5556936678")
    repo.claims_for_phone("5556936678")
    repo.claims_for_phone("5556936678")

    contact_queries = [q for q in fake.queries if "FROM Contact" in q]
    assert len(contact_queries) == 1


# --- write ----------------------------------------------------------------
def test_interaction_upserts_by_external_id(org):
    repo, fake = org
    record = InteractionRecord(
        call_id="01a14a45-c776-7001",
        caller_phone="5556936678",
        caller_name="Ana Duarte",
        summary="Caller was told the claim was denied and asked for a person.",
        sentiment="negative",
        intent="claim_status, escalation",
        authenticated=True,
        escalated=True,
        outcome="escalated_to_agent",
        claim_ids="CLM-48402",
        duration_seconds=179.8,
    )
    repo.write_interaction(record)
    repo.write_interaction(record)  # webhook redelivery

    # Two PATCHes to the same external id is still one Task — that is what the
    # external id buys, and it is why this needs no lock.
    assert len(fake.patches) == 2
    assert {p["call_id"] for p in fake.patches} == {"01a14a45-c776-7001"}

    body = fake.patches[0]["body"]
    assert body["WhoId"] == CONTACT["Id"]
    assert body["Status"] == "Completed"
    # TaskSubtype is insert-only in Salesforce; sending it breaks the second
    # write of a call, so it must never appear in an upsert body.
    assert "TaskSubtype" not in body
    assert body["Call_Sentiment__c"] == "negative"
    assert body["Identity_Verified__c"] is True
    assert body["Escalated__c"] is True
    assert body["CallDurationInSeconds"] == 179
    assert "denied" in body["Description"]


def test_the_callback_number_is_in_the_description_not_the_contact_match(org):
    """A number only present in the transcript is a callback nobody makes.

    It stays out of the contact lookup on purpose: it is unverified and need
    not be the number on the policy, so matching a Task to a customer on it
    could file the call against the wrong person.
    """
    repo, fake = org
    repo.write_interaction(InteractionRecord(
        call_id="01a14f07-b862-7bb9",
        caller_phone="",                 # web call: no caller ID at all
        callback_number="8072593908",
        caller_name="Unverified caller",
        summary="Caller could not recall the number on the account.",
        outcome="callback_scheduled",
        escalated=True,
    ))

    body = fake.patches[0]["body"]
    assert "Call back on: 8072593908" in body["Description"]
    assert "WhoId" not in body


def test_a_racing_write_retries_and_succeeds(org, monkeypatch):
    """Both write paths can upsert in the same second; one loses the insert."""
    repo, fake = org
    monkeypatch.setattr("app.repositories.salesforce.time.sleep", lambda _s: None)

    calls = {"n": 0}
    original = fake.handler

    def racy(request: httpx.Request) -> httpx.Response:
        if "/sobjects/Task/Call_Id__c/" in str(request.url):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(400, json=[{
                    "message": "duplicate value found: Call_Id__c duplicates value on record "
                               "with id: 00TXX0000012345",
                    "errorCode": "DUPLICATE_VALUE", "fields": []}])
        return original(request)

    repo._client = httpx.Client(transport=httpx.MockTransport(racy))  # noqa: SLF001
    repo.write_interaction(InteractionRecord(call_id="raced", summary="Both paths at once."))

    assert calls["n"] == 2, "the loser of the race must retry"


def test_a_write_that_fails_for_another_reason_is_not_retried(org):
    repo, fake = org

    def broken(request: httpx.Request) -> httpx.Response:
        if "/sobjects/Task/Call_Id__c/" in str(request.url):
            return httpx.Response(400, json=[{"errorCode": "REQUIRED_FIELD_MISSING",
                                              "message": "Required fields are missing"}])
        return fake.handler(request)

    repo._client = httpx.Client(transport=httpx.MockTransport(broken))  # noqa: SLF001
    with pytest.raises(Exception, match="REQUIRED_FIELD_MISSING"):
        repo.write_interaction(InteractionRecord(call_id="broken", summary="x"))


def test_the_summary_and_transcript_share_the_description(org):
    """Activity forbids Long Text Area custom fields, so both go in Description."""
    repo, fake = org
    transcript = "Aria: Thanks for calling Observe Insurance.\nCaller: I want to check my claim."
    repo.write_interaction(InteractionRecord(
        call_id="c-transcript", caller_phone="5556936678",
        summary="Checked a claim.", transcript=transcript,
        recording_url="https://example.com/recording.wav",
    ))
    description = fake.patches[0]["body"]["Description"]
    # Summary first — it is what gets read.
    assert description.startswith("Checked a claim.")
    assert "--- Transcript ---" in description
    assert "I want to check my claim" in description
    assert fake.patches[0]["body"]["Recording_Url__c"].endswith("recording.wav")


def test_a_very_long_transcript_is_truncated_not_rejected(org):
    repo, fake = org
    repo.write_interaction(InteractionRecord(
        call_id="c-long", summary="Long call.", transcript="x" * 50_000))
    description = fake.patches[0]["body"]["Description"]
    # Over the field limit the whole write fails, so the transcript gives way
    # and the summary survives.
    assert len(description) <= 32_000
    assert description.startswith("Long call.")
    assert description.endswith("[truncated]")


def test_empty_fields_are_omitted_rather_than_sent_as_blanks(org):
    repo, fake = org
    repo.write_interaction(InteractionRecord(call_id="c1", summary="Short call."))
    body = fake.patches[0]["body"]
    assert "Recording_Url__c" not in body
    assert "WhoId" not in body


# --- resilience -----------------------------------------------------------
def test_an_early_401_refreshes_the_token_and_retries_once(org):
    repo, fake = org
    repo.find_customer_by_phone("5556936678")   # primes a token
    assert fake.token_requests == 1

    fake.expire_next_call = True
    repo._contact_cache.clear()  # noqa: SLF001
    customer = repo.find_customer_by_phone("5556936678")

    assert customer is not None, "the retry should have succeeded"
    assert fake.token_requests == 2, "a dead session must force a refresh"


def test_health_reports_the_remaining_api_quota(org):
    repo, _ = org
    health = repo.health()
    assert health["ok"] is True
    assert health["api_calls_remaining"] == 14873
    assert health["instance"] == INSTANCE


# --- safety ---------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("5552341187", "5552341187"),
    ("o'brien", "o\\'brien"),
    ("back\\slash", "back\\\\slash"),
])
def test_soql_values_are_escaped(raw, expected):
    """A quote in a value would otherwise end the literal and change the query."""
    assert soql_escape(raw) == expected

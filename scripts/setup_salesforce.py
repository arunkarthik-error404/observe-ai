"""Provision and seed a Salesforce org from the same config as everything else.

    python scripts/setup_salesforce.py --check    # auth + schema + a read
    python scripts/setup_salesforce.py --schema   # custom fields + field permissions
    python scripts/setup_salesforce.py --seed     # Contacts and Cases from app/seed.py
    python scripts/setup_salesforce.py --all

Why script the schema instead of clicking it: nineteen custom fields across
three objects is tedious to create by hand, easy to typo, and impossible to
review. More importantly, **fields created through the API have no field-level
security by default** — they exist, queries name them, and every value comes
back null, which looks exactly like a mapping bug. So this also creates a
permission set granting read/edit on each field and assigns it to the running
user. That is the step most people lose an afternoon to.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import normalize_phone  # noqa: E402
from app.repositories.salesforce_auth import SalesforceAuthError, tokens  # noqa: E402
from app.seed import CLAIMS, CUSTOMERS  # noqa: E402

PERMISSION_SET = "Observe_Claims_Agent"

# object -> list of field definitions for the Tooling API
FIELDS: dict[str, list[dict[str, Any]]] = {
    "Contact": [
        {"name": "Normalized_Phone__c", "label": "Normalized Phone", "type": "Text",
         "length": 10, "externalId": True, "unique": True},
        {"name": "Policy_Id__c", "label": "Policy Id", "type": "Text", "length": 32},
    ],
    "Case": [
        {"name": "Claim_Number__c", "label": "Claim Number", "type": "Text",
         "length": 32, "externalId": True, "unique": True},
        {"name": "Claim_Status__c", "label": "Claim Status", "type": "Text", "length": 40},
        {"name": "Claim_Amount__c", "label": "Claim Amount", "type": "Currency",
         "precision": 16, "scale": 2},
        {"name": "Adjuster_Name__c", "label": "Adjuster Name", "type": "Text", "length": 80},
        {"name": "Required_Documents__c", "label": "Required Documents", "type": "Text", "length": 255},
        {"name": "Submission_Deadline__c", "label": "Submission Deadline", "type": "Date"},
        {"name": "Next_Step__c", "label": "Next Step", "type": "Text", "length": 255},
    ],
    "Task": [
        {"name": "Call_Id__c", "label": "Call Id", "type": "Text",
         "length": 64, "externalId": True, "unique": True},
        # The caller's name as the call recorded it. `WhoId` already points at
        # the Contact when the phone matched one, which is the better handle —
        # but it is only set on a match, so an unverified caller, or a verified
        # one ringing from a number no Contact carries, left the record with no
        # name on it at all. The brief asks for the name on the record, and a
        # lookup that is sometimes absent does not satisfy that. 121 = Contact
        # Name's own length (FirstName 40 + space + LastName 80).
        {"name": "Caller_Name__c", "label": "Caller Name", "type": "Text", "length": 121},
        {"name": "Call_Sentiment__c", "label": "Call Sentiment", "type": "Text", "length": 20},
        {"name": "Call_Intent__c", "label": "Call Intent", "type": "Text", "length": 255},
        # When the call ended, to the second. `ActivityDate` is a Date, so the
        # time of day was being truncated away — a 09:14 call and a 17:52 call
        # were indistinguishable on the record. `CreatedDate` is not a
        # substitute: it is when the row was written, and because both write
        # paths upsert the same Task it is pinned to whichever landed first.
        {"name": "Call_Timestamp__c", "label": "Call Timestamp", "type": "DateTime"},
        # How the call resolved. Previously this lived only in the Subject,
        # which is a problem because Subject is sent on every write while an
        # empty custom field is omitted: a second write with no outcome
        # overwrote "Claims call - status_provided" with a generic one. In its
        # own field, an outcome we do not have leaves the stored one alone.
        {"name": "Call_Outcome__c", "label": "Call Outcome", "type": "Text", "length": 200},
        {"name": "Identity_Verified__c", "label": "Identity Verified", "type": "Checkbox"},
        {"name": "Escalated__c", "label": "Escalated", "type": "Checkbox"},
        {"name": "Claim_Numbers__c", "label": "Claim Numbers", "type": "Text", "length": 255},
        # Which knowledge-base entries the agent read from, with the document
        # and section each one came from. "Where did the agent get that from"
        # is a compliance question in insurance, and the Sheets backend has
        # carried the answer since the column was added to INTERACTION_HEADERS.
        # Salesforce did not: this field did not exist and the Task payload had
        # no key for it, so on the live backend the provenance was computed,
        # attached to the session, sent to the escalation ticket — and then
        # dropped at the write. Nothing failed, which is why it survived; the
        # test that covers provenance asserts against MemoryRepository, which
        # stores the record object whole and so cannot see a missing field.
        {"name": "KB_Consulted__c", "label": "KB Consulted", "type": "Text", "length": 255},
        {"name": "Recording_Url__c", "label": "Recording URL", "type": "Url"},
        # No custom field for the transcript: Activity (the entity behind Task
        # and Event) does not allow Long Text Area custom fields at all. The
        # standard Description is already a 32k long text, is on every Task
        # layout by default, and is where a human looks for call notes — so the
        # summary and transcript go there together.
    ],
}

OK, BAD, SKIP = "  [ok]  ", "  [FAIL]", "  [skip]"

# Task and Event share one underlying metadata entity, "Activity". Creating a
# custom field on Task directly fails with a misleading restricted-picklist
# error; the field has to be defined on Activity and is then visible on both.
# Field *permissions*, confusingly, still use the concrete Task name.
METADATA_ENTITY = {"Task": "Activity", "Event": "Activity"}


class Org:
    def __init__(self) -> None:
        self.token, self.instance = tokens.credentials()
        self.version = settings().sf_api_version
        self.client = httpx.Client(
            timeout=30.0, headers={"Authorization": f"Bearer {self.token}"}
        )

    def call(self, method: str, path: str, **kwargs) -> Any:
        response = self.client.request(method, f"{self.instance}{path}", **kwargs)
        if response.status_code >= 300:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        return response.json() if response.content else {}

    def query(self, soql: str) -> list[dict[str, Any]]:
        return self.call("GET", f"/services/data/{self.version}/query",
                         params={"q": soql}).get("records", [])

    def tooling(self, method: str, path: str, **kwargs) -> Any:
        return self.call(method, f"/services/data/{self.version}/tooling{path}", **kwargs)


# --- schema ---------------------------------------------------------------
def field_metadata(spec: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {"label": spec["label"], "type": spec["type"]}
    if spec["type"] == "Text":
        meta["length"] = spec.get("length", 80)
    if spec["type"] == "Currency":
        meta.update(precision=spec.get("precision", 16), scale=spec.get("scale", 2))
    if spec["type"] == "LongTextArea":
        meta.update(length=spec.get("length", 32768),
                    visibleLines=spec.get("visibleLines", 10))
    if spec.get("externalId"):
        meta["externalId"] = True
    if spec.get("unique"):
        meta["unique"] = True
    if spec["type"] == "Checkbox":
        meta["defaultValue"] = "false"
    return meta


def ensure_schema(org: Org) -> None:
    print("\n-- custom fields --")
    created: list[str] = []
    failures: list[str] = []
    for sobject, specs in FIELDS.items():
        entity = METADATA_ENTITY.get(sobject, sobject)
        for spec in specs:
            metadata_name = f"{entity}.{spec['name']}"
            permission_name = f"{sobject}.{spec['name']}"
            try:
                org.tooling("POST", "/sobjects/CustomField",
                            json={"FullName": metadata_name, "Metadata": field_metadata(spec)})
                print(f"{OK} created {metadata_name}")
                created.append(permission_name)
            except RuntimeError as exc:
                if "DUPLICATE" in str(exc).upper() or "already exists" in str(exc):
                    print(f"{SKIP} {metadata_name} already exists")
                    created.append(permission_name)
                else:
                    # Keep going: one bad field should not hide the state of
                    # the other twelve, and the summary is more useful than a
                    # stack trace halfway through.
                    print(f"{BAD} {metadata_name}: {str(exc)[:220]}")
                    failures.append(metadata_name)

    ensure_field_permissions(org, created)
    if failures:
        print(f"\n{BAD} {len(failures)} field(s) not created: {', '.join(failures)}")
        raise SystemExit(1)


def ensure_field_permissions(org: Org, fields: list[str]) -> None:
    """Grant read/edit on every custom field, via a permission set.

    Without this the fields exist but read back as null for everyone, which is
    indistinguishable from a broken field mapping.
    """
    print("\n-- field level security --")
    existing = org.query(
        f"SELECT Id FROM PermissionSet WHERE Name = '{PERMISSION_SET}' LIMIT 1")
    if existing:
        perm_set_id = existing[0]["Id"]
        print(f"{SKIP} permission set {PERMISSION_SET} exists")
    else:
        perm_set_id = org.call("POST", f"/services/data/{org.version}/sobjects/PermissionSet",
                               json={"Name": PERMISSION_SET,
                                     "Label": "Observe Claims Agent"})["id"]
        print(f"{OK} created permission set {PERMISSION_SET}")

    granted = {
        r["Field"] for r in org.query(
            f"SELECT Field FROM FieldPermissions WHERE ParentId = '{perm_set_id}'")
    }
    for full_name in fields:
        if full_name in granted:
            continue
        sobject = full_name.split(".")[0]
        try:
            org.call("POST", f"/services/data/{org.version}/sobjects/FieldPermissions",
                     json={"ParentId": perm_set_id, "SobjectType": sobject,
                           "Field": full_name, "PermissionsRead": True,
                           "PermissionsEdit": True})
            print(f"{OK} granted {full_name}")
        except RuntimeError as exc:
            print(f"{BAD} {full_name}: {str(exc)[:200]}")

    user = org.query("SELECT Id, Username FROM User WHERE Id = '" + whoami(org) + "'")
    assignee = user[0]["Id"] if user else ""
    already = org.query(
        f"SELECT Id FROM PermissionSetAssignment "
        f"WHERE PermissionSetId = '{perm_set_id}' AND AssigneeId = '{assignee}'")
    if already:
        print(f"{SKIP} already assigned to {user[0]['Username']}")
    else:
        org.call("POST", f"/services/data/{org.version}/sobjects/PermissionSetAssignment",
                 json={"PermissionSetId": perm_set_id, "AssigneeId": assignee})
        print(f"{OK} assigned to {user[0]['Username']}")


def whoami(org: Org) -> str:
    identity = org.call("GET", f"/services/data/{org.version}/")
    # /services/data/vXX.X/ does not return the user; use the identity endpoint.
    del identity
    info = org.client.get(f"{org.instance}/services/oauth2/userinfo").json()
    return info.get("user_id", "")


# --- seed -----------------------------------------------------------------
def seed(org: Org) -> None:
    print("\n-- contacts --")
    contact_ids: dict[str, str] = {}
    for row in CUSTOMERS:
        phone = normalize_phone(row["phone"])
        first, _, last = row["full_name"].partition(" ")
        body = {
            "FirstName": first,
            "LastName": last or first,
            "Phone": row["phone"],
            "Birthdate": row["date_of_birth"],
            "Email": row.get("email", ""),
            "Policy_Id__c": row.get("policy_id", ""),
        }
        org.call("PATCH",
                 f"/services/data/{org.version}/sobjects/Contact/Normalized_Phone__c/{phone}",
                 json=body)
        found = org.query(
            f"SELECT Id FROM Contact WHERE Normalized_Phone__c = '{phone}' LIMIT 1")
        contact_ids[phone] = found[0]["Id"] if found else ""
        print(f"{OK} {row['full_name']:16} {row['phone']}  -> {contact_ids[phone]}")

    print("\n-- cases --")
    for row in CLAIMS:
        phone = normalize_phone(row["phone"])
        closed = row["status"].lower() in {"paid", "closed", "denied"}
        body = {
            "ContactId": contact_ids.get(phone, ""),
            "Status": "Closed" if closed else "Working",
            "Claim_Status__c": row["status"],
            "Subject": row["claim_type"],
            "Type": row["claim_type"],
            "Description": row["status_detail"],
            "Adjuster_Name__c": row["adjuster_name"],
            "Required_Documents__c": row["required_documents"],
            "Submission_Deadline__c": row["submission_deadline"] or None,
            "Next_Step__c": row["next_step"],
            "Origin": "Phone",
        }
        amount = row["amount"].replace("$", "").replace(",", "")
        if amount:
            body["Claim_Amount__c"] = float(amount)
        body = {k: v for k, v in body.items() if v not in (None, "")}
        org.call("PATCH",
                 f"/services/data/{org.version}/sobjects/Case/Claim_Number__c/{row['claim_id']}",
                 json=body)
        print(f"{OK} {row['claim_id']:12} {row['status']:20} {row['claim_type']}")


# --- check ----------------------------------------------------------------
def check(org: Org) -> int:
    from app.repositories.salesforce import SalesforceRepository

    print(f"\n{OK} authenticated to {org.instance}")
    limits = org.call("GET", f"/services/data/{org.version}/limits")
    print(f"         api calls remaining today: {limits.get('DailyApiRequests', {}).get('Remaining')}")

    missing = []
    for sobject, specs in FIELDS.items():
        described = org.call("GET", f"/services/data/{org.version}/sobjects/{sobject}/describe")
        names = {f["name"] for f in described["fields"]}
        for spec in specs:
            if spec["name"] not in names:
                missing.append(f"{sobject}.{spec['name']}")
    if missing:
        print(f"{BAD} missing fields: {', '.join(missing)}")
        print("         run: python scripts/setup_salesforce.py --schema")
        return 1
    print(f"{OK} all {sum(len(v) for v in FIELDS.values())} custom fields present")

    repo = SalesforceRepository()
    customer = repo.find_customer_by_phone("555-234-1187")
    if customer is None:
        print(f"{BAD} no Contact for 555-234-1187 — run --seed")
        return 1
    claims = repo.claims_for_phone(customer.phone)
    print(f"{OK} lookup works: {customer.full_name} ({customer.policy_id}) with {len(claims)} claim(s)")
    for claim in claims:
        print(f"         {claim.claim_id:12} {claim.status:20} {claim.next_step[:40]}")
    if not claims:
        print(f"{BAD} Contact found but no Cases — check field permissions, then re-run --seed")
        return 1

    print("\nall checks passed — set BACKEND=salesforce to run the agent on it\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", action="store_true")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if not any([args.schema, args.seed, args.check, args.all]):
        parser.error("pick at least one of --schema, --seed, --check, --all")

    cfg = settings()
    if not cfg.sf_client_id:
        print("SF_CLIENT_ID is not set — see docs/SALESFORCE.md", file=sys.stderr)
        return 2

    try:
        org = Org()
    except SalesforceAuthError as exc:
        print(f"{BAD} authentication failed\n         {exc}", file=sys.stderr)
        print("         check SF_CLIENT_ID / SF_CLIENT_SECRET, and that the Connected App\n"
              "         has the client-credentials flow enabled with a 'Run As' user.",
              file=sys.stderr)
        return 1

    if args.schema or args.all:
        ensure_schema(org)
    if args.seed or args.all:
        seed(org)
    if args.check or args.all:
        return check(org)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

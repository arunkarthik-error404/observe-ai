"""Replay a scenario against a running server, exactly as Vapi would.

Useful for rehearsing without voice minutes, for warming the debug console
before a demo, and for smoke-testing a fresh deploy:

    python scripts/simulate_call.py --base http://127.0.0.1:8000 --scenario happy
    python scripts/simulate_call.py --base https://<service>.onrender.com --scenario all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

import httpx

Step = tuple[str, dict[str, Any]]  # optional 3rd element: the squad member's name

SCENARIOS: dict[str, list[Step]] = {
    "happy": [
        ("lookup_customer", {"phone_number": "555-234-1187"}),
        ("verify_identity", {"date_of_birth": "November 3 1979"}),
        ("get_claim_status", {}),
        ("search_knowledge_base", {"query": "how do I upload another photo later"}),
        ("log_interaction", {"summary": "Daniel checked claim CLM-48201; inspection on the 20th.",
                             "sentiment": "positive", "outcome": "status_provided"}),
    ],
    "documents": [
        ("lookup_customer", {"phone_number": "5553612234"}),
        ("verify_identity", {"date_of_birth": "04/12/1988"}),
        ("get_claim_status", {}),
        ("get_document_instructions", {}),
        ("log_interaction", {"summary": "Priya told which two documents to upload and the deadline.",
                             "sentiment": "neutral", "outcome": "documents_explained"}),
    ],
    "auth_failure": [
        ("lookup_customer", {"phone_number": "555-418-4492"}),
        ("verify_identity", {"date_of_birth": "1965-03-28"}),
        ("verify_identity", {"date_of_birth": "1965-02-27"}),
        ("verify_identity", {"date_of_birth": "February 1965"}),
        ("get_claim_status", {}),
        ("escalate_to_human", {"reason": "authentication_failed", "urgency": "normal",
                               "notes": "Caller could not confirm the date of birth on file."}),
    ],
    "not_found": [
        ("lookup_customer", {"phone_number": "555-999-8888"}),
        ("search_knowledge_base", {"query": "how do I start a new claim"}),
        ("search_knowledge_base", {"query": "what are your office hours"}),
        ("escalate_to_human", {"reason": "customer_not_found", "notes": "No account for 5559998888."}),
    ],
    "escalation": [
        ("lookup_customer", {"phone_number": "555-693-6678"}),
        ("verify_identity", {"date_of_birth": "01/09/1982"}),
        ("get_claim_status", {}),
        ("search_knowledge_base", {"query": "can I appeal a denial"}),
        ("escalate_to_human", {"reason": "caller_request", "urgency": "high",
                               "notes": "Wants to appeal the out-of-network denial with a person."}),
    ],
    "emergency": [
        ("lookup_customer", {"phone_number": "555-234-1187"}),
        ("report_emergency", {"description": "my kitchen is on fire right now"}),
    ],
    # Squad mode: the same call id passes between three assistants. Proves the
    # specialist inherits the verification the triage agent performed.
    "squad": [
        ("lookup_customer", {"phone_number": "555-693-6678"}, "observe-triage"),
        ("verify_identity", {"date_of_birth": "January 9 1982"}, "observe-triage"),
        ("get_claim_status", {}, "observe-claims-specialist"),
        ("search_knowledge_base", {"query": "can I appeal a denial"}, "observe-claims-specialist"),
        ("escalate_to_human", {"reason": "caller_request", "urgency": "high",
                               "notes": "Wants to appeal the denial with a person."},
         "observe-escalation"),
        ("log_interaction", {"summary": "Ana was told her claim was denied and asked for an agent.",
                             "sentiment": "frustrated", "outcome": "escalated_to_agent"},
         "observe-escalation"),
    ],
}


def run(base: str, scenario: str, secret: str, verbose: bool) -> None:
    call_id = f"sim_{scenario}_{uuid.uuid4().hex[:8]}"
    headers = {"x-vapi-secret": secret} if secret else {}
    print(f"\n=== {scenario}  ({call_id}) ===")

    with httpx.Client(base_url=base, timeout=30.0, headers=headers) as client:
        for step in SCENARIOS[scenario]:
            name, arguments = step[0], step[1]
            assistant = step[2] if len(step) > 2 else ""
            message: dict[str, Any] = {
                "type": "tool-calls",
                "call": {"id": call_id},
                "toolCallList": [{"id": f"tc_{uuid.uuid4().hex[:8]}", "name": name, "arguments": arguments}],
            }
            if assistant:
                message["assistant"] = {"name": assistant}
            body = {"message": message}
            started = time.perf_counter()
            response = client.post("/vapi/webhook", json=body)
            elapsed = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            result = response.json()["results"][0]["result"]

            flag = "ok " if result.get("ok") else f"ERR({result.get('error')})"
            who = f"[{assistant.replace('observe-', '')}] " if assistant else ""
            print(f"  {who}{name:28} {flag:22} {elapsed:6.0f} ms")
            if verbose:
                print(json.dumps(result, indent=4)[:1500])
            if result.get("instruction") and not verbose:
                print(f"      -> {result['instruction'][:120]}")

        report = {"message": {
            "type": "end-of-call-report",
            "call": {"id": call_id},
            "durationSeconds": 84.0,
            "analysis": {
                "summary": f"Simulated {scenario} scenario.",
                "structuredData": {"sentiment": "neutral", "outcome": scenario},
            },
        }}
        response = client.post("/vapi/webhook", json=report)
        print(f"  end-of-call-report           -> {response.status_code} {response.text[:120]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--scenario", default="all", choices=["all", *SCENARIOS])
    parser.add_argument("--secret", default="", help="x-vapi-secret, if the target enforces one")
    parser.add_argument("-v", "--verbose", action="store_true", help="print full tool results")
    args = parser.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    try:
        for name in names:
            run(args.base, name, args.secret, args.verbose)
    except httpx.HTTPError as exc:
        print(f"\nrequest failed: {exc}", file=sys.stderr)
        return 1
    print(f"\nopen {args.base}/debug to inspect these calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Provision the Vapi assistants and squad from vapi/agents.json.

Why this exists: an agent that was clicked together in a dashboard cannot be
reviewed, diffed, or rebuilt. Here the prompts are files, the tool schemas are
config, and this script reconciles them into Vapi over the API — so "show us
the agent configuration" is a `git diff`, and a broken experiment is one
re-provision away from fixed.

Usage
-----
    python scripts/provision_vapi.py --mode single           # one assistant
    python scripts/provision_vapi.py --mode squad            # 3-agent squad
    python scripts/provision_vapi.py --mode single --dry-run # print payloads

Environment
-----------
    VAPI_PRIVATE_KEY   Vapi private API key (dashboard > API keys)
    SERVER_URL         Public https base URL of this backend, no trailing slash
    VAPI_SECRET        Shared secret sent back as x-vapi-secret on every call
    TRANSFER_PHONE_NUMBER  Optional E.164 number for live warm transfers
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")  # real env vars still win, for CI and Render

VAPI_DIR = ROOT / "vapi"
API = "https://api.vapi.ai"
INCLUDE = re.compile(r"<!--\s*include:\s*(?P<file>[\w./-]+)\s*-->")


# --- prompt composition ---------------------------------------------------
def render_prompt(relative: str) -> str:
    """Inline `<!-- include: x.md -->` so shared rules live in exactly one file."""
    path = VAPI_DIR / relative
    text = path.read_text(encoding="utf-8")

    def substitute(match: re.Match[str]) -> str:
        return render_prompt(str(Path(relative).parent / match.group("file")))

    return INCLUDE.sub(substitute, text).strip()


# --- payload building -----------------------------------------------------
def _update_env(values: dict[str, str]) -> None:
    """Rewrite existing keys in .env in place. Only touches keys already there,
    so nothing is reordered, commented out, or accidentally invented."""
    path = ROOT / ".env"
    if not path.exists() or not values:
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            new_line = f"{key}={values[key]}"
            if new_line != line:
                lines[index] = new_line
                changed = True
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  .env updated: {', '.join(values)}")


def webhook_url(server_url: str, secret: str) -> str:
    """The webhook URL, with the secret in the path when there is one.

    Used for tools *and* assistant-level messages. Vapi redacts `server.secret`
    on read, so there is no way to confirm from the API whether it was stored —
    and the one time that mattered, it had not been, which silently 401'd the
    post-call write-back. Carrying the credential in the path removes the
    guesswork: the header is still accepted, this just means auth does not
    depend on it.
    """
    return f"{server_url}/vapi/webhook/{secret}" if secret else f"{server_url}/vapi/webhook"


def tool_payload(name: str, spec: dict[str, Any], server_url: str, secret: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
        },
        # Per-tool server config keeps every tool on one endpoint and one secret.
        "server": {"url": webhook_url(server_url, secret), "secret": secret, "timeoutSeconds": 15},
    }
    if spec.get("request_start"):
        # Spoken the instant the tool fires, so a 300ms lookup never sounds like
        # dead air. This is perceived-latency work, not real latency work.
        payload["messages"] = [{"type": "request-start", "content": spec["request_start"]}]
    return payload


def analysis_plan(flat: dict[str, Any]) -> dict[str, Any]:
    """Translate the flat config into Vapi's nested analysisPlan shape."""
    plan: dict[str, Any] = {}
    if flat.get("summaryPrompt"):
        plan["summaryPlan"] = {
            "enabled": True,
            "messages": [
                {"role": "system", "content": flat["summaryPrompt"]},
                {"role": "user", "content": "Here is the transcript:\n\n{{transcript}}\n\n"},
            ],
        }
    if flat.get("structuredDataSchema"):
        plan["structuredDataPlan"] = {
            "enabled": True,
            "schema": flat["structuredDataSchema"],
            "messages": [
                {
                    "role": "system",
                    "content": flat.get("structuredDataPrompt", "Extract the post-call record."),
                },
                {"role": "user", "content": "Here is the transcript:\n\n{{transcript}}\n\n"},
            ],
        }
    if flat.get("successEvaluationPrompt"):
        plan["successEvaluationPlan"] = {
            "enabled": True,
            "rubric": flat.get("successEvaluationRubric", "PassFail"),
            "messages": [
                {"role": "system", "content": flat["successEvaluationPrompt"]},
                {"role": "user", "content": "Here is the transcript:\n\n{{transcript}}\n\n"},
            ],
        }
    return plan


def assistant_payload(name: str, spec: dict[str, Any], config: dict[str, Any],
                      server_url: str, secret: str,
                      tool_ids: dict[str, str] | None = None) -> dict[str, Any]:
    """Build an assistant.

    Function tools are *referenced* by id rather than embedded: they are shared
    Tool objects in Vapi, created from this same config, so all four assistants
    point at one definition of `lookup_customer` instead of four copies. The
    source of truth is still `vapi/agents.json` — the script reconciles the
    shared objects on every run, so they cannot drift from the repo.
    """
    defaults = config["defaults"]
    tool_ids = tool_ids or {}
    # Anything that is not a shared function tool still rides inline.
    inline_tools: list[dict[str, Any]] = []

    transfer_number = os.getenv("TRANSFER_PHONE_NUMBER", "").strip()
    if transfer_number and "escalate_to_human" in spec.get("tools", []):
        inline_tools.append({
            "type": "transferCall",
            "destinations": [{
                "type": "number",
                "number": transfer_number,
                "message": "Connecting you to a representative now. Please hold.",
                "description": "A human claims representative.",
            }],
        })

    payload: dict[str, Any] = {
        "name": name,
        "model": {
            **defaults["model"],
            "messages": [{"role": "system", "content": render_prompt(spec["prompt"])}],
            # `tools` is sent even when empty: it clears any inline definitions
            # left over from before the switch to shared Tool objects, which
            # would otherwise sit alongside the referenced ones as duplicates.
            "tools": inline_tools,
            "toolIds": [tool_ids[t] for t in spec.get("tools", []) if t in tool_ids],
        },
        "voice": defaults["voice"],
        "transcriber": defaults["transcriber"],
        # Assistant-level messages (status-update, end-of-call-report) are
        # authenticated by the URL as well as the secret: Vapi sends the
        # per-tool secret but was observed not to send this one, which 401'd
        # the post-call write-back. Keeping `secret` set costs nothing and
        # starts working the moment Vapi does send it.
        "server": {"url": webhook_url(server_url, secret), "secret": secret,
                   "timeoutSeconds": 15},
        "serverMessages": defaults["serverMessages"],
        "startSpeakingPlan": defaults["startSpeakingPlan"],
        "stopSpeakingPlan": defaults["stopSpeakingPlan"],
        "messagePlan": defaults["messagePlan"],
        "silenceTimeoutSeconds": defaults["silenceTimeoutSeconds"],
        "maxDurationSeconds": defaults["maxDurationSeconds"],
        "backgroundSound": defaults["backgroundSound"],
        "endCallFunctionEnabled": defaults["endCallFunctionEnabled"],
        "endCallMessage": defaults["endCallMessage"],
        "artifactPlan": {"recordingEnabled": bool(defaults.get("recordingEnabled", True))},
        "analysisPlan": analysis_plan(defaults["analysisPlan"]),
    }

    if spec.get("firstMessage"):
        payload["firstMessage"] = spec["firstMessage"]
    else:
        # Squad members after the entry point must not re-greet the caller, so
        # they open with a model-generated continuation instead of a fixed line.
        payload["firstMessageMode"] = "assistant-speaks-first-with-model-generated-message"
    return payload


# --- api ------------------------------------------------------------------
class Vapi:
    def __init__(self, key: str) -> None:
        self.client = httpx.Client(
            base_url=API, timeout=30.0, headers={"Authorization": f"Bearer {key}"}
        )

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 300:
            print(f"  !! {response.request.method} {response.request.url.path} "
                  f"-> {response.status_code}\n     {response.text[:900]}", file=sys.stderr)
            response.raise_for_status()
        return response.json()

    def list(self, resource: str) -> list[dict[str, Any]]:
        data = self._json(self.client.get(f"/{resource}", params={"limit": 200}))
        return data if isinstance(data, list) else data.get("results", [])

    def patch(self, resource: str, identifier: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(self.client.patch(f"/{resource}/{identifier}", json=payload))

    def upsert(self, resource: str, name: str, payload: dict[str, Any],
               name_of: Callable[[dict[str, Any]], str | None] = lambda item: item.get("name"),
               ) -> dict[str, Any]:
        """Reconcile by name, so re-running is safe and never duplicates."""
        existing = next((item for item in self.list(resource) if name_of(item) == name), None)
        if existing:
            print(f"  patch {resource}/{existing['id']}  ({name})")
            return self._json(self.client.patch(f"/{resource}/{existing['id']}", json=payload))
        print(f"  create {resource}  ({name})")
        return self._json(self.client.post(f"/{resource}", json=payload))


def tool_name_of(item: dict[str, Any]) -> str | None:
    """A Tool object's name lives under `function`, not at the top level."""
    return (item.get("function") or {}).get("name") or item.get("name")


def attach_phone_number(api: Vapi, mode: str, ids: dict[str, str], squad_name: str) -> None:
    """Point the inbound number at whatever this run provisioned.

    A number routes to exactly one destination, so `--mode` decides: provision
    single and callers reach the one assistant, provision squad and they land
    on triage. Both keys are always sent — assigning one without clearing the
    other leaves a number pointing at two things.
    """
    number_id = os.getenv("VAPI_PHONE_NUMBER_ID", "").strip()
    if not number_id:
        return
    target = ids[squad_name] if mode == "squad" else ids["observe-claims-agent"]
    payload = ({"squadId": target, "assistantId": None} if mode == "squad"
               else {"assistantId": target, "squadId": None})
    number = api.patch("phone-number", number_id, payload)
    print(f"  phone {number.get('number')} -> {mode} ({target})")


# --- entry point ----------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["single", "squad"], default="single")
    parser.add_argument("--dry-run", action="store_true", help="print payloads instead of calling the API")
    args = parser.parse_args()

    config = json.loads((VAPI_DIR / "agents.json").read_text(encoding="utf-8"))
    server_url = os.getenv("SERVER_URL", "").rstrip("/")
    secret = os.getenv("VAPI_SECRET", "")
    key = os.getenv("VAPI_PRIVATE_KEY", "")

    if not server_url:
        print("SERVER_URL is required (e.g. https://observe-claims.onrender.com)", file=sys.stderr)
        return 2
    if not secret:
        print("warning: VAPI_SECRET is empty — webhook requests will be unauthenticated", file=sys.stderr)

    wanted = {name: spec for name, spec in config["assistants"].items() if spec["mode"] == args.mode}
    needed_tools = sorted({t for spec in wanted.values() for t in spec.get("tools", [])})
    tool_payloads = {t: tool_payload(t, config["tools"][t], server_url, secret) for t in needed_tools}

    if args.dry_run:
        print(json.dumps({
            "tools": tool_payloads,
            "assistants": {
                name: assistant_payload(name, spec, config, server_url, secret,
                                        {t: f"<id:{t}>" for t in needed_tools})
                for name, spec in wanted.items()
            },
        }, indent=2))
        return 0
    if not key:
        print("VAPI_PRIVATE_KEY is required (or use --dry-run)", file=sys.stderr)
        return 2

    api = Vapi(key)
    print(f"provisioning {args.mode} mode -> {server_url}")

    # Tools first: assistants reference them by id, so they have to exist.
    # Reconciling them every run is what stops the shared objects in Vapi from
    # drifting away from the definitions in this repo.
    tool_ids: dict[str, str] = {}
    for tool, payload in tool_payloads.items():
        tool_ids[tool] = api.upsert("tool", tool, payload, name_of=tool_name_of)["id"]

    ids: dict[str, str] = {}
    for name, spec in wanted.items():
        payload = assistant_payload(name, spec, config, server_url, secret, tool_ids)
        ids[name] = api.upsert("assistant", name, payload)["id"]

    if args.mode == "squad":
        squad_cfg = config["squad"]
        order = [squad_cfg["entry"]] + [m for m in squad_cfg["members"] if m != squad_cfg["entry"]]
        members = []
        for member in order:
            destinations = [
                {
                    "type": "assistant",
                    "assistantName": dest["assistant"],
                    "message": dest.get("message", ""),
                    "description": dest["description"],
                }
                for dest in config["assistants"][member].get("destinations", [])
            ]
            entry: dict[str, Any] = {"assistantId": ids[member]}
            if destinations:
                entry["assistantDestinations"] = destinations
            members.append(entry)
        squad = api.upsert("squad", squad_cfg["name"], {"name": squad_cfg["name"], "members": members})
        ids[squad_cfg["name"]] = squad["id"]

    attach_phone_number(api, args.mode, ids, config["squad"]["name"])

    # Write the ids back so the browser dialler at /call picks them up without
    # anyone copying UUIDs by hand.
    updates = {}
    if args.mode == "single":
        updates["VAPI_ASSISTANT_ID"] = ids.get("observe-claims-agent", "")
    else:
        updates["VAPI_SQUAD_ID"] = ids.get(config["squad"]["name"], "")
    _update_env({k: v for k, v in updates.items() if v})

    print("\ndone:")
    for name, identifier in ids.items():
        print(f"  {name:32} {identifier}")
    print("\nStart a web call from the Vapi dashboard, then watch "
          f"{server_url}/debug")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

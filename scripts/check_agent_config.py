"""Static checks on the agent configuration, before it ever reaches Vapi.

The test suite proves the *server* behaves. Nothing in it proves the agent is
wired correctly, because the wiring lives in `vapi/agents.json` and the prompt
files — and a mistake there is invisible until a caller hits it. Provisioning
a broken config succeeds; you find out on the phone.

So this checks the seams between the config and the code:

  * tool parity — a tool the model can call but the server does not implement
    returns `unknown_tool` mid-call, and a tool the server implements but no
    assistant lists is dead weight nobody will notice is missing.
  * schemas — a tool with no description is a tool the model guesses about.
  * routing — a destination pointing at an assistant that is not in the squad
    strands the caller.
  * prompts — an include that does not resolve ships the literal comment to
    the model as instructions.

The last group are regression guards for bugs that actually happened on live
calls. They are written here, rather than left as comments, because that is
the difference between a lesson and a check:

    python scripts/check_agent_config.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Importing the tool registry must not touch a real backend or a real .env.
os.environ.setdefault("BACKEND", "memory")

from app.tools import REGISTRY  # noqa: E402
from scripts.provision_vapi import INCLUDE, render_prompt  # noqa: E402

AGENTS = ROOT / "vapi" / "agents.json"

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    if not ok:
        failures.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    config = json.loads(AGENTS.read_text(encoding="utf-8"))
    tools = {k: v for k, v in config["tools"].items() if not k.startswith("_")}
    assistants = config["assistants"]
    squad = config["squad"]

    # --- tools -----------------------------------------------------------
    print("-- tools --")
    missing_in_code = sorted(set(tools) - set(REGISTRY))
    missing_in_config = sorted(set(REGISTRY) - set(tools))
    check(not missing_in_code, "every configured tool is implemented", f"no handler: {missing_in_code}")
    check(not missing_in_config, "every implemented tool is configured", f"unreachable: {missing_in_config}")

    for name, spec in sorted(tools.items()):
        has_description = bool(spec.get("description", "").strip())
        params = spec.get("parameters", {})
        well_formed = params.get("type") == "object" and isinstance(params.get("properties"), dict)
        check(has_description and well_formed, f"{name}: description and parameter schema",
              "a tool the model cannot read is a tool it will misuse")

    # --- assistants ------------------------------------------------------
    print("-- assistants --")
    for name, assistant in assistants.items():
        listed = assistant.get("tools", [])
        undefined = sorted(set(listed) - set(tools))
        check(not undefined, f"{name}: tools all exist", f"undefined: {undefined}")

        prompt_path = ROOT / "vapi" / assistant["prompt"]
        check(prompt_path.exists(), f"{name}: prompt file exists", str(prompt_path))
        if prompt_path.exists():
            rendered = render_prompt(assistant["prompt"])
            check(len(rendered) > 200, f"{name}: prompt renders", f"{len(rendered)} chars")
            check(not INCLUDE.search(rendered), f"{name}: all includes resolved",
                  "an unresolved include ships an HTML comment as instructions")

        # A crisis can be declared at any point in a call, including while the
        # caller is being handed over. Every agent must be able to route one.
        check("report_emergency" in listed, f"{name}: can handle an emergency")

    # --- squad routing ---------------------------------------------------
    print("-- squad --")
    members = squad["members"]
    check(squad["entry"] in members, "entry assistant is a squad member", squad["entry"])
    for member in members:
        check(member in assistants, f"member {member} is defined")
        check(assistants.get(member, {}).get("mode") == "squad", f"member {member} is marked squad mode")

    for member in members:
        for destination in assistants.get(member, {}).get("destinations", []):
            target = destination["assistant"]
            check(target in members, f"{member} -> {target}: target is in the squad")
            check(target != member, f"{member} -> {target}: no self-transfer")
            # Vapi picks the destination by matching this text, so an empty
            # description makes routing a coin flip.
            check(bool(destination.get("description", "").strip()),
                  f"{member} -> {target}: has a routing description")
            # Regression guard, from a live call: a transfer message makes the
            # agent announce the handoff, and the receiving agent then greets
            # the caller a second time. Internal routing stays internal.
            check(destination.get("message", "") == "", f"{member} -> {target}: silent transfer",
                  "a non-empty message announces the internal handoff to the caller")

    # Regression guard, from a live call: when the specialist could also
    # escalate, the caller heard the handoff announced twice. In squad mode
    # exactly one agent owns escalation.
    owners = [m for m in members if "escalate_to_human" in assistants.get(m, {}).get("tools", [])]
    check(len(owners) == 1, "exactly one squad agent owns escalation", f"owners: {owners}")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

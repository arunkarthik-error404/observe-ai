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
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Importing the tool registry must not touch a real backend or a real .env.
os.environ.setdefault("BACKEND", "memory")

from app.seed import CUSTOMERS  # noqa: E402
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
        check(bool(assistants.get(member, {}).get("prompt")), f"member {member} has a prompt")

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

    # --- prompts against the tools they are running ----------------------
    #
    # Everything above this checks wiring: names that resolve, destinations
    # that exist, includes that render. None of it can see a prompt that is
    # internally coherent and contradicts the server. That gap ran live for
    # several commits — `verify_identity` was changed to hold the verification
    # sentence back for the agent that stays, `squad_claims.md` was given the
    # duty, and `squad_triage.md` was left telling the triage agent to say it
    # before transferring. Both instructions were in the same prompt at once,
    # and the model picked one per call.
    print("-- prompts vs tools --")
    rendered: dict[str, str] = {}
    for name, assistant in assistants.items():
        relative = assistant.get("prompt")
        if relative and (ROOT / "vapi" / relative).exists():
            rendered[name] = render_prompt(relative)

    # Nothing meant for an editor may reach the model. A `why` block whose own
    # text contained a comment-closing sequence ended early and shipped the
    # rest of itself as instructions — the pattern is non-greedy, so it stopped
    # at the example rather than the real terminator.
    for name, text in rendered.items():
        check("<!-- why" not in text, f"{name}: no unclosed why block",
              "a why block did not terminate; its text is now in the prompt")
        check("-->" not in text, f"{name}: no comment residue",
              "a stray comment terminator means a block closed in the wrong place")

    # These prompts teach by counter-example — "Wrong: ..." lines quote the
    # thing the model must not say. Scanning raw text for a forbidden phrase
    # therefore flags the prompt for forbidding it, which is backwards. Every
    # check below reads the instructions only.
    def instructions_only(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().lower().startswith("wrong:")
        )

    # The architecture, asserted rather than described: whoever authenticates
    # the caller must also be the one who answers them. This is the check the
    # old three-agent squad would have failed — `observe-triage` held
    # `verify_identity` and nothing else, so the call had to change hands in
    # the middle of one continuous exchange, and the 18 Oct recording put
    # about eight seconds of silence in that gap. Splitting at verification is
    # now a failing build rather than a judgement call.
    verifiers = [m for m, a in assistants.items() if "verify_identity" in (a.get("tools") or [])]
    check(len(verifiers) == 1, "exactly one agent authenticates", f"verifiers: {verifiers}")
    for name in verifiers:
        tools = assistants[name].get("tools") or []
        check("get_claim_status" in tools,
              f"{name}: the agent that verifies also serves the claim",
              "an agent that authenticates and then hands the caller on splits one "
              "continuous exchange across a transfer; the caller pays for it in silence")

    # The verification sentence is held back by `verify_identity` for the first
    # tool after it, so no prompt may hand it out at the verification step.
    # Asserted positively — that the prompt tells the model to announce
    # nothing there — because an absence of the wrong phrasing is not the
    # presence of the right one.
    for name in verifiers:
        body = instructions_only(rendered.get(name, "")).lower()
        check("do not announce it here" in body,
              f"{name}: announces nothing at the verification step",
              "verify_identity returns the name but tells the model to hold it; a prompt "
              "that says to greet them by name there contradicts the tool, and the model "
              "followed the prompt — naming a caller four seconds before the check ran")

    # A worked example using a name from the demo dataset puts that exact name
    # in the model's context next to the sentence it belongs in. A live call
    # thanked a caller by name before `verify_identity` had run at all, and
    # the only place that name appeared in any prompt was an example.
    seed_names = {c["preferred_name"] for c in CUSTOMERS} | {
        c["full_name"].split()[-1] for c in CUSTOMERS
    }
    for name, text in rendered.items():
        body = instructions_only(text)
        found = sorted(n for n in seed_names if re.search(rf"\b{re.escape(n)}\b", body))
        check(not found, f"{name}: no demo persona names in the instructions",
              f"found {found} — use the field name, e.g. `greeting_name`")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

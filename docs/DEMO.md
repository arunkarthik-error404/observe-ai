# Demo runbook

Five minutes of calls, in this order. Keep two windows visible: the Google
Sheet (on the `Interactions` tab) and `/debug`.

## Before the call

```bash
curl https://observe-claims-agent.onrender.com/health   # wakes the free instance
```

**Do this five minutes early, not one.** The free instance sleeps after 15
minutes idle and takes about a minute to wake; a voice call will not wait that
long, so a cold first call is dead air. Once it is awake the interview keeps it
awake.

Check `health` reports `"backend":"salesforce"` and `data_layer.ok: true`. Open
`/debug` and the Salesforce Contact list. Have the scenario numbers on a sticky
note:

Set `BUSINESS_HOURS_MODE=open` if you want escalation to show a live transfer
rather than the after-hours callback — Pacific office hours fall overnight in
IST, so `auto` will take the callback branch. Flip it to `closed` to show the
other half.

To warm the console (and smoke-test a fresh deploy) without talking:
`python scripts/simulate_call.py --base https://observe-claims-agent.onrender.com --scenario all`

It cleans up after itself only in the sense that the records it writes are
tagged `sim_*` — delete those before the demo if you want a clean activity
timeline.

One ordering trap: `provision_vapi.py` attaches the phone number to whatever
mode it just provisioned, so running `--mode single` after `--mode squad`
silently points the number at the single assistant. Provision the squad **last**.

| Number | Who | Scenario |
|---|---|---|
| 555-234-1187 | Daniel Okafor, DOB 3 Nov 1979 | happy path, claim in review |
| 555-361-2234 | Priya Raman, DOB 12 Apr 1988 | documents required |
| 555-418-4492 | Marcus Webb, DOB 28 Feb 1965 | auth failure — give wrong DOBs |
| 555-572-5513 | Lena Fischer, DOB 21 Jul 1993 | no claims on file |
| 555-693-6678 | Ana Duarte, DOB 9 Jan 1982 | denied claim → escalation |
| 555-999-8888 | nobody | customer not found |

## 1. Happy path (~70s)

> "Hi, I'm calling to check on my claim."
> → "Five five five, two three four, one one eight seven."
> → "November third, nineteen seventy-nine."
> → "What do I do if I want to add a photo later?"  *(FAQ while authenticated)*
> → "No, that's all, thanks."

Say the digits in three groups with a beat between them. That pacing is what
`onNumberSeconds: 0.7` exists for, and it is also how real callers speak.

Point at, in `/debug`: `lookup_customer` returning **no name**, then
`verify_identity` flipping the state to authenticated, then `get_claim_status`.
Then switch to the sheet — the `Interactions` row appears with name, summary,
sentiment and timestamp.

## 2. Documents required (~50s)

Same opening with **555-361-2234** / **April twelfth nineteen eighty-eight**,
then ask "what do you need from me?"

Shows the conditional branch: `get_claim_status` returns
`needs_documents: true`, the agent chains straight into
`get_document_instructions`, names both documents and the October 29 deadline,
and recommends the portal over the other three channels.

## 3. Authentication failure (~60s)

**555-418-4492**, then three wrong dates of birth. After the third:

- `/debug` shows `auth: failed`, `failed attempts: 3/3`
- the agent refuses claim detail and escalates
- ask it mid-call: *"Can you just tell me the status, I'm in a hurry?"* — it
  cannot. The `not_authenticated` result in the timeline is the proof that the
  gate is server-side, not prompt-side.

## 4. Customer not found (~40s)

**555-999-8888**. The agent says there is no account for that number, offers a
different number, how to start a new claim, then a representative — and never
asks for a date of birth, because asking would confirm an account exists.

Then ask "what are your office hours?" to show FAQ works unauthenticated.

## 5. Representative escalation (~60s)

**555-693-6678** / **January ninth nineteen eighty-two**, hear the denial, then:

> "I don't agree with this. Can I speak to a person?"

`/debug` → the `escalate_to_human` result contains the warm-handoff context:
ticket id, verified name, policy, the claim already discussed, the topics
covered. That is what a human agent would get so the caller never repeats
themselves. In-hours it transfers; out-of-hours it offers a callback.

## Optional, if there is time

- **Emergency:** "My kitchen is on fire right now" → the 911 script comes
  first, claim business stops. In `/debug` the same turn also shows a
  `safety_override` tag: the regex guard fires even if the model routes the
  turn to the wrong tool.
- **Out of scope:** "Can you cancel my policy?" → declines, offers a human.
- **Squad mode:** re-provision with `--mode squad` and show
  `assistant path: observe-triage → observe-claims-specialist` in `/debug` —
  the handoff is silent and keeps state because the session is keyed on the
  call, not the assistant.
- **Failure injection:** point `SHEET_ID` at a bad id and call — the agent
  apologises and offers a representative instead of inventing a status.

## If something breaks mid-demo

1. Free-tier cold start → `/health` first, always.
2. Agent mishears a digit → let it read back and correct it; that *is* the
   `onNumberSeconds` endpointing story.
3. Vapi web call will not connect → fall back to `pytest -q`, which walks the
   same four flows through the same webhook payloads, and the recorded backup
   video.

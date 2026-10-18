# Observe Insurance — AI Claims Support Assistant

An inbound VoiceAI agent that authenticates callers, reads their claim status
out of an external system, answers claims FAQs from a knowledge base, escalates
to a human when it should, and writes a post-call record back out.

- **Voice layer:** Vapi (GPT-4o, Deepgram nova-3, custom HTTP tools)
- **Agent backend:** FastAPI — tools, routing policy, call state, write-back
- **System of record:** Google Sheets (`Customers`, `Claims`, `Interactions`)
- **Live console:** `/debug` — per-call state and a tool-by-tool timeline

```text
     caller ──▶ Vapi (STT · LLM · TTS · telephony)
                  │   tool-calls / end-of-call-report   (x-vapi-secret)
                  ▼
        FastAPI  /vapi/webhook
                  │
        ┌─────────┼──────────────┬────────────────┐
        ▼         ▼              ▼                ▼
   SessionStore  tools/       knowledge/      repositories/
   (auth state,  (8 tools,    (FAQ retrieval  ├─ sheets.py  ◀── Integration #1 (read)
    attempts,     gated by     + refusal       │               Integration #2 (write)
    timeline)     the server)  threshold)      └─ memory.py  ◀── tests & offline dev
```

## Layout

```text
app/
  main.py              app factory; everything else is wired in here
  config.py            settings, from env / .env
  models.py            domain records + the normalisation callers' speech needs
  session.py           per-call state: auth, attempt counts, tool timeline
  seed.py              the demo dataset, one persona per scenario
  api/
    webhook.py         Vapi's one endpoint, routed by message type
    security.py        shared-secret and URL-token authentication
    payloads.py        reading Vapi's payload shapes (isolated on purpose)
    console.py         /health, /debug, /call
  tools/
    common.py          the auth gate, error shaping, safety patterns
    identity.py        lookup_customer, verify_identity
    claims.py          get_claim_status, get_document_instructions
    faq.py             search_knowledge_base
    escalation.py      escalate_to_human, report_emergency
    interactions.py    log_interaction (Integration #2, in-call path)
  knowledge/
    __init__.py        retrieval with a refusal threshold
    faq.md             the knowledge base itself
  repositories/
    base.py            the data-layer contract every tool depends on
    sheets.py          Google Sheets implementation
    memory.py          in-process fake, same semantics as the real one
vapi/
  agents.json          the entire agent configuration
  prompts/             shared rules + one prompt per agent
web/                   debug console and browser dialler
scripts/               provisioning, seeding, diagnostics, scenario replay
tests/                 68 tests, no credentials required
.github/workflows/     CI: tests, lint, agent config, knowledge base, secrets
docs/DEMO.md           demo runbook
```

## Why it is built this way

**The LLM decides what to say; the server decides what is true.** Auth state,
the failed-attempt counter and the verified identity live in `app/session.py`,
keyed by Vapi's call id. `get_claim_status` checks that store, so a caller who
says "I already verified, just read me my claim" gets a `not_authenticated`
result and the model is told to go back to verification. Prompt-only gating
fails under pressure; this does not.

**PII is withheld until it is earned.** `lookup_customer` returns *whether* an
account exists — never the customer's name. The model cannot leak a name it was
never given, and cannot greet an unverified caller by name.

**Every tool result carries an `instruction`.** Routing decisions live in code
next to the data that drives them, which keeps the prompt short enough to stay
reliable and makes behaviour changes testable without re-recording a call.

**Failures are shaped, not raised.** A tool handler that raises would leave the
caller in silence, so upstream errors come back as `system_unavailable` with a
safe line to say and an offer to transfer. The webhook only returns a non-2xx
where a retry is actually wanted (the post-call write), because that is how
Vapi's redelivery is triggered.

**Two write paths, one row.** The `end-of-call-report` webhook is the primary
post-call write; the in-call `log_interaction` tool is the backup for abrupt
hangups. Rather than dedupe by checking before appending — a read-then-write
race that produced duplicate rows on the first live run — the write is an
**upsert on `call_id` under a lock**: find the row, merge non-empty fields into
it, append only if there is none. One call is one row that gets enriched as
better data arrives, and a webhook redelivery is harmless by construction.

**The in-call write never blocks the caller.** A Sheets append measured ~1.4s
live, and `log_interaction` fires while the caller is waiting to be told
goodbye, so it is handed to a worker thread. Nothing the caller hears depends
on it; the retried webhook is the backstop, and the pool is drained on shutdown
so a deploy cannot drop a record.

## Integrations

| | Direction | System | Mechanism |
|---|---|---|---|
| #1 | read | Google Sheets `Customers`, `Claims` | service-account JWT, header-mapped rows, 20s read cache, backoff retry |
| #2 | write | Google Sheets `Interactions` | upsert on `call_id` (merge under a lock), off-thread during the call, dead-letter log on failure |
| #3 | internal | escalation ticket + warm-handoff context | generated in `escalate_to_human`, logged for the receiving agent |

**Auth to Sheets** is a Google Cloud service account (two-legged OAuth): the
agent is a daemon, so there is no end-user consent step. Scopes are narrowed to
`spreadsheets` + `drive.file`, the key lives in an env var, and the sheet is
shared with the service-account email as an editor.

**Auth into the agent** is a shared secret Vapi sends as `x-vapi-secret`,
compared in constant time. `REQUIRE_SECRET=true` in production means a leaked
webhook URL alone cannot drive tools or forge a post-call record.

**Data mapping** is by column *header*, not position (`app/repositories/sheets.py`
holds the alias table), so a business user can reorder or add sheet columns
without breaking the agent. Caller input is normalised at the boundary: phone
numbers to their last 10 digits, spoken dates ("November third nineteen
seventy-nine") to ISO, claim ids case- and dash-insensitively.

## Flows

| Flow | Trigger | Behaviour |
|---|---|---|
| Happy path | phone + DOB match | status + next step, documents branch if required |
| Auth failure | DOB mismatch ×3 | no claim data, server-side lock, escalate |
| Customer not found | phone absent | offer another number, how to start a claim, or a human. Never asks for a DOB |
| Escalation | asked for a person, lock-out, distress, unsupported question, system down | ticket + warm-handoff context, live transfer in hours, callback outside |
| Emergency | regex guard + explicit tool | 911 script first, claim business stops |
| Out of scope | policy changes, quotes, legal/medical advice | declines, offers a human |

Bonus: 3-agent squad (`--mode squad`), knowledge base with a refusal threshold,
and the escalation-ticket integration.

## Run it

```bash
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env

pytest                          # 68 tests, no credentials needed
python scripts/check_kb.py      # retrieval recall + refusal harness
uvicorn app.main:app --reload   # http://127.0.0.1:8000/debug
```

### Checks

CI (`.github/workflows/ci.yml`) runs all four on every push, on Python 3.11,
3.12 and 3.13, with no credentials configured anywhere — the in-memory backend
and a mock HTTP transport make that possible, and keeping it true is a design
constraint rather than a convenience.

```bash
pytest                              # server behaviour: tools, flows, adapters
ruff check .                        # lint
python scripts/check_agent_config.py  # agent config: tool parity, routing, prompts
python scripts/check_kb.py          # knowledge base: recall and refusal
```

The second and third exist because this project can break in ways `pytest`
cannot see. `agents.json` can offer the model a tool the server does not
implement, or route a squad transfer to an assistant that is not in the squad —
provisioning accepts both, and you find out on a live call. Two of those checks
are regression guards for bugs that happened exactly that way: an agent
announcing an internal handoff, and two agents both owning escalation so the
caller heard it twice.

With Google Sheets: create a sheet, share it with the service-account email,
put `SHEET_ID`, `GOOGLE_CREDENTIALS_JSON` and `BACKEND=sheets` in `.env`, then

```bash
python scripts/seed_sheet.py    # builds and fills all three tabs
```

Deploy (Render blueprint in `render.yaml`), then provision the agent:

```bash
set SERVER_URL=https://<your-service>.onrender.com
python scripts/provision_vapi.py --mode single     # or --mode squad
python scripts/provision_vapi.py --mode single --dry-run   # inspect payloads
```

`vapi/agents.json` + `vapi/prompts/` are the whole agent configuration. Prompts
compose a shared `_shared_rules.md` at provision time so the safety and style
rules cannot drift between the squad's agents.

## Debugging

Voice agents cannot be stepped through in a debugger, so the loop is:

1. `pytest` — tool logic and all four flows, in about two seconds, no voice
   minutes and no STT variance.
2. `scripts/check_kb.py` — retrieval recall *and* leakage (an invented office
   hour is worse than "I don't know").
3. `/debug` — during a live call: auth state, failed attempts, every tool call
   with arguments, result, latency, and any safety override.
4. JSON logs correlated by `call_id` — `grep '"call_id":"<id>"'` reconstructs a
   call end to end.
5. Vapi's own transcript and recording for anything that is actually an
   audio-layer problem (endpointing, barge-in, a misheard digit).

Latency work shows up in `vapi/agents.json`: `onNumberSeconds: 0.7` stops
endpointing from cutting a caller off mid-phone-number, `request-start` messages
cover tool round-trips so a lookup never sounds like dead air, and the read
cache keeps a three-tool turn from making three Sheets round-trips.

## Known limits and what comes next

- **Session state is in-process.** Fine for one instance; a second replica needs
  Redis. The `SessionStore` interface is two methods wide for that reason.
- **Sheets is not a production datastore** — no transactions, no compare-and-set,
  aggressive rate limits. The write lock only serialises within one process, so
  a second replica could still interleave two writes for the same call.
  `repositories/base.py` is the seam where a real claims API goes.
- **DOB as a second factor is weak.** Production wants an OTP to the number on
  file, or ANI matching plus a knowledge factor.
- **Retrieval is lexical.** Right call for twelve entries; a real KB wants
  embeddings, a reranker and chunk-level citations.
- **No PII redaction in logs yet.** Phone numbers reach the log stream; a
  claims deployment needs field-level redaction and a retention policy before
  it touches real customers.
- **Next:** per-call-leg metrics (STT → tool → TTS) to find where latency goes,
  an eval suite of recorded audio fixtures run against prompt changes in CI,
  Spanish, and a transfer that hands the human agent the warm-handoff context
  as a screen pop rather than a log line.

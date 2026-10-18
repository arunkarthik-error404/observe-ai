"""Integration #3: handing an escalation to a human, visibly.

A ticket written only to a log is a promise nobody can see. This posts the
warm-handoff context into Slack the moment the agent escalates, so a human
sees who is calling, whether they were verified, what was already discussed,
and what they want — before they pick up.

Three rules, the same ones the Sheets write-back follows:

* **Never block the caller.** The post happens on a worker thread. A slow
  Slack must not add latency to a conversation.
* **Never fail the call.** If Slack is down, misconfigured, or rate-limited,
  the escalation still succeeds — the agent has already told the caller what
  happens next, and that must stay true.
* **No-op when unconfigured.** With `SLACK_WEBHOOK_URL` empty this does
  nothing at all, so tests and offline development are unaffected.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("claims.notify")

# Separate from the interaction writer: a backed-up notifier should never be
# able to delay a post-call record, and vice versa.
NOTIFIER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-notifier")

URGENCY_EMOJI = {"high": "🔴", "normal": "🟠", "low": "🟡"}


def is_configured() -> bool:
    return bool(settings().slack_webhook_url.strip())


def build_message(context: dict[str, Any], transfer: str) -> dict[str, Any]:
    """Build the Slack payload. Pure, so it can be asserted on in tests."""
    verified = bool(context.get("identity_verified"))
    name = context.get("customer_name") or "Unverified caller"
    urgency = str(context.get("urgency") or "normal").lower()
    claims = context.get("claims_discussed") or []
    reason = str(context.get("reason") or "caller_request")

    # The human needs to know *immediately* whether to trust the name, so the
    # verification state is a field rather than something buried in the notes.
    identity = "✅ Verified" if verified else "⚠️ NOT verified — re-check before disclosing anything"
    window = context.get("callback_window", "pending")
    handoff = "Live transfer" if transfer == "live" else f"Callback — {window}"

    fields = [
        {"type": "mrkdwn", "text": f"*Caller*\n{name}"},
        {"type": "mrkdwn", "text": f"*Phone*\n{context.get('caller_phone') or 'unknown'}"},
        {"type": "mrkdwn", "text": f"*Identity*\n{identity}"},
        {"type": "mrkdwn", "text": f"*Policy*\n{context.get('policy_id') or '—'}"},
        {"type": "mrkdwn", "text": f"*Claims discussed*\n{', '.join(claims) if claims else '—'}"},
        {"type": "mrkdwn", "text": f"*Handoff*\n{handoff}"},
    ]

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{URGENCY_EMOJI.get(urgency, '🟠')} Escalation — {reason.replace('_', ' ')}",
            },
        },
        {"type": "section", "fields": fields},
    ]

    if context.get("notes"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*What they said*\n>{context['notes']}"},
        })

    topics = context.get("topics") or []
    attempts = context.get("auth_attempts", 0)
    footer = f"Ticket {context.get('ticket_id')} · {urgency} urgency"
    if topics:
        footer += f" · covered: {', '.join(topics)}"
    if attempts:
        footer += f" · {attempts} failed ID attempt(s)"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    return {
        # `text` is the notification preview and the accessibility fallback —
        # without it Slack shows "This content can't be displayed".
        "text": f"Escalation {context.get('ticket_id')}: {name} needs a representative ({reason})",
        "blocks": blocks,
    }


def _post(payload: dict[str, Any], ticket_id: str) -> None:
    url = settings().slack_webhook_url.strip()
    try:
        response = httpx.post(url, json=payload, timeout=6.0)
        if response.status_code >= 300:
            log.error("slack_post_failed",
                      extra={"ticket": ticket_id, "status": response.status_code,
                             "body": response.text[:200]})
        else:
            log.info("slack_notified", extra={"ticket": ticket_id})
    except Exception as exc:  # noqa: BLE001 - a notifier must never raise into a call
        log.error("slack_post_error", extra={"ticket": ticket_id, "error": str(exc)})


def notify_escalation(context: dict[str, Any], transfer: str) -> bool:
    """Queue a Slack notification. Returns whether one was queued."""
    if not is_configured():
        return False
    NOTIFIER.submit(_post, build_message(context, transfer), str(context.get("ticket_id", "")))
    return True

"""Operator-facing surfaces: health, the live call console, and the dialler.

None of these are part of the agent. They exist because a voice agent cannot
be stepped through in a debugger, so the only way to understand a call is to
watch its state change while it happens.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from .. import notifications
from ..config import settings
from ..models import utc_now_iso
from ..redaction import redact_snapshot
from ..repositories import repository
from ..session import store
from .security import console_unmasked, require_console_access

router = APIRouter(tags=["console"])
WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# The debug console polls /health every 1.5s, and the data layer's health check
# is a live API call. Against Salesforce that burned ~2,400 requests an hour
# out of a 15,000/day limit — a monitoring endpoint quietly eating the quota it
# exists to protect. Cached for 30s: still fresh enough to notice an outage
# during a demo, cheap enough to leave a dashboard open all day.
_HEALTH_TTL = 30.0
_health_cache: tuple[float, dict] | None = None


def _data_layer_health() -> dict:
    global _health_cache
    now = time.time()
    if _health_cache and now - _health_cache[0] < _HEALTH_TTL:
        return {**_health_cache[1], "cached": True}
    result = repository().health()
    _health_cache = (now, result)
    return result


@router.get("/health")
def health(t: str | None = None, x_debug_token: str | None = Header(default=None)) -> dict[str, Any]:
    cfg = settings()
    layer = _data_layer_health()
    unmasked = console_unmasked(t, x_debug_token)
    body = {
        "ok": True,
        "backend": cfg.backend,
        "data_layer": layer,
        "secret_enforced": bool(cfg.vapi_secret),
        "slack_notifications": notifications.is_configured(),
        "time": utc_now_iso(),
    }
    if unmasked:
        # `active_calls` is the one field here that describes callers rather
        # than the service. Closing /debug/calls was about not telling an
        # anonymous reader that a call was happening — and a live count said
        # exactly that, from an endpoint the gate does not cover, which left
        # the disclosure public at a different URL. Omitted rather than
        # zeroed: a number that is always 0 is a number someone will trust.
        body["active_calls"] = len(store.all())
    else:
        # The org's My Domain URL and the remaining API quota are free
        # reconnaissance: one names the tenant, the other says how many calls
        # it would take to exhaust it. Liveness does not need either.
        body["data_layer"] = {
            k: v for k, v in layer.items() if k not in {"instance", "api_calls_remaining"}
        }
    return body


def _view(session, unmasked: bool) -> dict[str, Any]:
    """One place decides whether a snapshot keeps its PII.

    These endpoints had no authentication and returned `tool_events` with the
    raw arguments each tool was called with — and `verify_identity`'s argument
    is the caller's date of birth. Anyone could read a live caller's phone
    number, date of birth and verified name off the open internet: the exact
    pair this system treats as credentials. Masked is now the default for
    everyone, and the token lifts it rather than granting access, so a
    forgotten `DEBUG_TOKEN` leaves the console working and the data covered.
    """
    snapshot = session.snapshot()
    return snapshot if unmasked else redact_snapshot(snapshot)


@router.get("/debug/calls")
def debug_calls(request: Request, t: str | None = None,
                x_debug_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_console_access(t, x_debug_token, request)
    unmasked = console_unmasked(t, x_debug_token)
    sessions = store.all()
    return {
        "count": len(sessions),
        "redacted": not unmasked,
        "calls": [_view(s, unmasked) for s in sessions],
    }


@router.get("/debug/calls/{call_id}")
def debug_call(call_id: str, request: Request, t: str | None = None,
               x_debug_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_console_access(t, x_debug_token, request)
    session = store.peek(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such call")
    return _view(session, console_unmasked(t, x_debug_token))


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send the bare domain to the console.

    This is an API, so `/` had no route and FastAPI answered `{"detail":"Not
    Found"}` — correct, and indistinguishable from a broken deploy to anyone
    who opens the URL expecting a service to be there. The console is the page
    worth landing on anyway.
    """
    return RedirectResponse("/debug")


@router.get("/debug")
def debug_console() -> FileResponse:
    return FileResponse(WEB_DIR / "debug.html")


@router.get("/call")
def call_page() -> FileResponse:
    """The dialler the caller side of the demo runs from, instead of the Vapi
    dashboard. Keeps the demo on surfaces I control, and lets single-agent and
    squad mode be started side by side for comparison."""
    return FileResponse(WEB_DIR / "call.html")


@router.get("/call/config")
def call_config() -> dict[str, Any]:
    cfg = settings()
    return {
        "publicKey": cfg.vapi_public_key,
        "assistantId": cfg.vapi_assistant_id,
        "squadId": cfg.vapi_squad_id,
        "ready": bool(cfg.vapi_public_key and (cfg.vapi_assistant_id or cfg.vapi_squad_id)),
    }

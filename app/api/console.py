"""Operator-facing surfaces: health, the live call console, and the dialler.

None of these are part of the agent. They exist because a voice agent cannot
be stepped through in a debugger, so the only way to understand a call is to
watch its state change while it happens.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from .. import notifications
from ..config import settings
from ..models import utc_now_iso
from ..repositories import repository
from ..session import store

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
def health() -> dict[str, Any]:
    cfg = settings()
    return {
        "ok": True,
        "backend": cfg.backend,
        "data_layer": _data_layer_health(),
        "secret_enforced": bool(cfg.vapi_secret),
        "slack_notifications": notifications.is_configured(),
        "active_calls": len(store.all()),
        "time": utc_now_iso(),
    }


@router.get("/debug/calls")
def debug_calls() -> dict[str, Any]:
    return {"count": len(store.all()), "calls": [s.snapshot() for s in store.all()]}


@router.get("/debug/calls/{call_id}")
def debug_call(call_id: str) -> dict[str, Any]:
    session = store.peek(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such call")
    return session.snapshot()


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

"""Authenticating requests that claim to come from Vapi.

Two credentials are accepted, and either is sufficient:

* `x-vapi-secret`, the shared secret set on every tool's server config.
* The secret embedded in the webhook path, used for assistant-level messages.
  On the first live call Vapi sent the header for tool calls but not for
  `end-of-call-report`, which 401'd the post-call write-back. Rather than drop
  authentication on the one endpoint that writes records, the URL carries the
  credential — the standard fallback for a provider that will not send a header
  you can verify.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

from ..config import settings

log = logging.getLogger("claims.api")


def auth_header_names(request: Request) -> dict[str, str]:
    """Header names that could carry credentials, with values masked.

    Logged on every rejection. Without it, a provider that sends its secret
    under a different header (or not at all) is indistinguishable from a wrong
    secret, and the only symptom is a silently missing record.
    """
    interesting = ("x-vapi", "authorization", "signature", "secret", "token")
    return {
        name: f"{value[:4]}…{len(value)}ch" if value else ""
        for name, value in request.headers.items()
        if any(marker in name.lower() for marker in interesting)
    }


def token_matches(token: str) -> bool:
    cfg = settings()
    return bool(cfg.vapi_secret) and hmac.compare_digest(token, cfg.vapi_secret)


def console_unmasked(token: str | None, header: str | None) -> bool:
    """Whether this caller may see a call snapshot with the PII still in it.

    The operator console is useful without PII and dangerous with it, so the
    default is masked for everyone and the token only lifts the mask. That way
    a missing or mistyped `DEBUG_TOKEN` fails closed — the page still works and
    shows asterisks — rather than failing open on the one endpoint that can
    serve a caller's date of birth.

    With no token configured this returns False in production and True
    locally, because a developer debugging a value they cannot otherwise see is
    the whole reason the console exists.
    """
    cfg = settings()
    expected = cfg.debug_token.strip()
    if not expected:
        return not cfg.require_secret
    return any(
        supplied and hmac.compare_digest(supplied, expected)
        for supplied in (token, header)
    )


def require_secret(supplied: str | None, request: Request) -> None:
    cfg = settings()
    if not cfg.vapi_secret:
        if cfg.require_secret:
            raise HTTPException(status_code=500, detail="server secret not configured")
        return
    if supplied and hmac.compare_digest(supplied, cfg.vapi_secret):
        return
    log.warning("webhook_rejected", extra={
        "reason": "wrong_secret" if supplied else "no_secret_header",
        "auth_headers_seen": auth_header_names(request),
    })
    raise HTTPException(status_code=401, detail="invalid secret")

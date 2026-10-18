"""Server-to-server authentication against Salesforce.

Two flows, both two-legged — there is no user sitting in front of this agent to
click an consent screen, which is the same reason Google access uses a service
account rather than end-user OAuth.

* **client_credentials** (default) — the Connected App runs as a named
  integration user. One POST, a client id and secret, no certificates. This is
  the modern Salesforce answer for machine-to-machine and the least setup.
* **jwt_bearer** — a signed assertion proves identity using a certificate you
  uploaded to the Connected App. No shared secret ever leaves your side, which
  is what a security review would prefer, at the cost of key management.

Tokens are cached in memory and refreshed on expiry *or* on the first 401,
because Salesforce can invalidate a session early (admin logout, password
reset, session settings) and the expiry clock alone will not tell you.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("claims.salesforce")

# Salesforce does not return expires_in on every flow, so assume a conservative
# lifetime and let the 401 path be the real safety net.
ASSUMED_TOKEN_LIFETIME = 45 * 60


class SalesforceAuthError(RuntimeError):
    """Could not obtain an access token. Never retried blindly: almost always
    configuration (wrong secret, flow not enabled, user not pre-authorised)."""


class TokenProvider:
    """Fetches and caches an access token plus the instance URL to call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._instance_url: str = ""
        self._expires_at: float = 0.0

    # --- public ----------------------------------------------------------
    def credentials(self, force_refresh: bool = False) -> tuple[str, str]:
        """Return (access_token, instance_url), refreshing when needed."""
        with self._lock:
            if force_refresh or not self._token or time.time() >= self._expires_at:
                self._fetch_locked()
            return self._token or "", self._instance_url

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    # --- internals -------------------------------------------------------
    def _fetch_locked(self) -> None:
        cfg = settings()
        flow = cfg.sf_auth_flow
        if flow == "jwt_bearer":
            data = self._jwt_payload()
        else:
            data = {
                "grant_type": "client_credentials",
                "client_id": cfg.sf_client_id,
                "client_secret": cfg.sf_client_secret,
            }

        token_url = f"{cfg.sf_login_url.rstrip('/')}/services/oauth2/token"
        try:
            response = httpx.post(token_url, data=data, timeout=15.0)
        except httpx.HTTPError as exc:
            raise SalesforceAuthError(f"could not reach {token_url}: {exc}") from exc

        if response.status_code >= 300:
            # Salesforce returns {"error": "...", "error_description": "..."},
            # and the description is the only part that says what to fix.
            body = response.text[:400]
            raise SalesforceAuthError(
                f"{flow} token request failed ({response.status_code}): {body}"
            )

        payload: dict[str, Any] = response.json()
        self._token = payload.get("access_token", "")
        self._instance_url = payload.get("instance_url", "").rstrip("/")
        if not self._token or not self._instance_url:
            raise SalesforceAuthError(f"token response missing fields: {payload}")

        lifetime = int(payload.get("expires_in") or ASSUMED_TOKEN_LIFETIME)
        # Refresh a minute early so a call never fails on a token that expires
        # between the check and the request.
        self._expires_at = time.time() + max(60, lifetime - 60)
        log.info("salesforce_token_obtained",
                 extra={"flow": flow, "instance": self._instance_url})

    def _jwt_payload(self) -> dict[str, str]:
        try:
            import jwt  # PyJWT, only needed for this flow
        except ImportError as exc:  # pragma: no cover
            raise SalesforceAuthError(
                "jwt_bearer needs PyJWT and cryptography: pip install pyjwt cryptography"
            ) from exc

        cfg = settings()
        key = cfg.sf_private_key.replace("\\n", "\n") if cfg.sf_private_key else ""
        if not key and cfg.sf_private_key_file:
            from pathlib import Path

            path = Path(cfg.sf_private_key_file)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            key = path.read_text(encoding="utf-8")
        if not key:
            raise SalesforceAuthError("jwt_bearer needs SF_PRIVATE_KEY or SF_PRIVATE_KEY_FILE")

        assertion = jwt.encode(
            {
                "iss": cfg.sf_client_id,     # the Connected App's consumer key
                "sub": cfg.sf_username,      # the user to act as
                "aud": cfg.sf_login_url.rstrip("/"),
                "exp": int(time.time()) + 180,
            },
            key,
            algorithm="RS256",
        )
        return {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }


tokens = TokenProvider()

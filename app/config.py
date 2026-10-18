"""Runtime configuration, loaded from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        """Trim surrounding whitespace off every string setting.

        Credentials reach production by being pasted into a web form, and a
        multi-line paste box will happily keep the trailing newline. That cost
        a deploy once: SF_LOGIN_URL came back with a "\\n" on the end and every
        Salesforce call died on `Invalid non-printable ASCII character in URL`
        — an error that names the character but not the setting it came from.

        Stripping is safe for all of these: none is whitespace-significant at
        its edges, and a PEM key keeps the newlines in its middle.
        """
        return value.strip() if isinstance(value, str) else value

    # --- data layer -------------------------------------------------------
    # "sheets" talks to Google Sheets, "salesforce" to a Salesforce org, and
    # "memory" uses the seeded fake so the whole agent can be exercised (and
    # tested) with zero credentials. Nothing above this layer changes between
    # them — that is the point of the repository interface.
    backend: Literal["sheets", "salesforce", "memory"] = "memory"

    google_credentials_json: str = ""  # service-account JSON, inline
    google_credentials_file: str = ""  # ...or a path to it
    sheet_id: str = ""
    customers_tab: str = "Customers"
    claims_tab: str = "Claims"
    interactions_tab: str = "Interactions"

    # --- Salesforce (alternative system of record) ------------------------
    # login.salesforce.com for production/developer orgs,
    # test.salesforce.com for sandboxes.
    sf_login_url: str = "https://login.salesforce.com"
    sf_auth_flow: Literal["client_credentials", "jwt_bearer"] = "client_credentials"
    sf_client_id: str = ""      # Connected App consumer key
    sf_client_secret: str = ""  # client_credentials only
    sf_username: str = ""       # jwt_bearer only: the user to act as
    sf_private_key: str = ""    # jwt_bearer: PEM inline (\n escaped)
    sf_private_key_file: str = ""
    sf_api_version: str = "v62.0"
    sf_timeout_seconds: float = 10.0

    # --- security ---------------------------------------------------------
    # Shared secret Vapi sends as x-vapi-secret on every server request.
    vapi_secret: str = ""
    require_secret: bool = False

    # --- browser call page -------------------------------------------------
    # The *public* key is meant to be exposed in a browser; it can only start
    # calls, not read or change anything. The private key never leaves .env.
    vapi_public_key: str = ""
    # No assistant id: the squad is the only destination. VAPI_ASSISTANT_ID in
    # an older .env is now ignored rather than offering a second call target
    # that no longer resolves to anything.
    vapi_squad_id: str = ""

    # --- privacy ----------------------------------------------------------
    # Phone numbers, names and dates of birth are masked in the log stream.
    # Defaults on: a claims line handles regulated data, and logs outlive the
    # call. Turn it off locally to debug a value you cannot otherwise see —
    # never in an environment handling real customers.
    redact_logs: bool = True

    # Lifts the mask on /debug/calls and on the infrastructure fields in
    # /health. Empty means masked for everyone in production and visible
    # locally, so forgetting to set it fails closed.
    debug_token: str = ""

    # --- behaviour --------------------------------------------------------
    max_auth_attempts: int = 3
    # Guesses allowed against one phone number across every call, not just
    # within one. max_auth_attempts resets the moment a caller hangs up and
    # redials, which makes it a speed bump rather than a lockout.
    max_attempts_per_number: int = 9
    session_ttl_seconds: int = 3600
    sheets_timeout_seconds: float = 8.0
    sheets_max_retries: int = 3

    support_phone: str = "+1-800-555-0142"
    office_hours: str = "Monday to Friday, 8am to 6pm Pacific"

    # A live warm transfer needs somewhere to transfer *to*. With this unset,
    # escalation promises a callback instead of a connection it cannot make.
    transfer_phone_number: str = ""

    # Integration #3: where escalations are delivered to humans. Empty disables
    # the notifier entirely, so nothing breaks without it.
    slack_webhook_url: str = ""

    # Escalation branches on whether the human team is open. "auto" uses the
    # real Pacific clock; "open"/"closed" pin it so both the live-transfer and
    # the callback branch can be demonstrated from any timezone.
    business_hours_mode: Literal["auto", "open", "closed"] = "auto"


@lru_cache
def settings() -> Settings:
    return Settings()

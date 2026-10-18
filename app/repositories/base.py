"""The data-layer contract.

Every tool depends on this interface, never on Google Sheets directly. Sheets
is the demo system of record; a real deployment would point the same methods
at Guidewire / Salesforce / an internal claims service without the agent, the
prompts or the tools changing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Claim, Customer, InteractionRecord


class RepositoryError(RuntimeError):
    """Upstream system unavailable or malformed. Callers degrade gracefully."""


class ClaimsRepository(ABC):
    @abstractmethod
    def find_customer_by_phone(self, phone: str) -> Customer | None:
        """phone is normalised to 10 digits."""

    @abstractmethod
    def claims_for_phone(self, phone: str) -> list[Claim]:
        ...

    @abstractmethod
    def write_interaction(self, record: InteractionRecord) -> None:
        """Integration #2. Must be idempotent on record.call_id."""

    def health(self) -> dict:
        return {"backend": type(self).__name__, "ok": True}

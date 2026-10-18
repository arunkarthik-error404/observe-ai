"""In-process implementation of the data layer.

Used by the test suite and by `BACKEND=memory` local runs. It also fails on
demand (`fail_next`) so the retry / graceful-degradation paths can be
exercised without breaking Google Sheets on purpose.
"""

from __future__ import annotations

from ..models import Claim, Customer, InteractionRecord, normalize_phone
from ..seed import CLAIMS, CUSTOMERS
from .base import ClaimsRepository, RepositoryError


class MemoryRepository(ClaimsRepository):
    def __init__(self) -> None:
        self.customers: dict[str, Customer] = {}
        self.claims: list[Claim] = []
        self.interactions: dict[str, InteractionRecord] = {}
        self.fail_next = 0
        self.reset()

    def reset(self) -> None:
        self.customers = {
            normalize_phone(row["phone"]): Customer(**{**row, "phone": normalize_phone(row["phone"])})
            for row in CUSTOMERS
        }
        self.claims = [
            Claim(**{**row, "phone": normalize_phone(row["phone"])}) for row in CLAIMS
        ]
        self.interactions = {}
        self.fail_next = 0

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RepositoryError("simulated upstream failure")

    def find_customer_by_phone(self, phone: str) -> Customer | None:
        self._maybe_fail()
        return self.customers.get(normalize_phone(phone))

    def claims_for_phone(self, phone: str) -> list[Claim]:
        self._maybe_fail()
        key = normalize_phone(phone)
        return [c for c in self.claims if c.phone == key]

    def write_interaction(self, record: InteractionRecord) -> None:
        """Upsert on call_id, merging non-empty fields — same contract as Sheets.

        Keeping the fake's semantics identical matters: the first version of
        this just overwrote by key, which made the test suite pass while the
        real Sheets write was appending duplicate rows.
        """
        self._maybe_fail()
        existing = self.interactions.get(record.call_id)
        if existing is None:
            self.interactions[record.call_id] = record
            return
        merged = existing.model_dump()
        for field, value in record.model_dump().items():
            if value not in (None, "", False):
                merged[field] = value
        self.interactions[record.call_id] = InteractionRecord(**merged)

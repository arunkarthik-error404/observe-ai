"""Data-layer factory. One switch picks the system of record."""

from functools import lru_cache

from ..config import settings
from .base import ClaimsRepository, RepositoryError
from .memory import MemoryRepository
from .salesforce import SalesforceRepository
from .sheets import SheetsRepository

__all__ = [
    "ClaimsRepository", "RepositoryError", "MemoryRepository",
    "SheetsRepository", "SalesforceRepository", "repository",
]

BACKENDS = {
    "sheets": SheetsRepository,
    "salesforce": SalesforceRepository,
    "memory": MemoryRepository,
}


@lru_cache
def repository() -> ClaimsRepository:
    """The one place the system of record is chosen. Nothing above this line
    knows which it is — tools depend on the interface, not the implementation."""
    return BACKENDS.get(settings().backend, MemoryRepository)()

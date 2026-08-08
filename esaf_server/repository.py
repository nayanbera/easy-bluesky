"""Abstract repository interfaces and factory function."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import ESAFRecord, PIGroup


class ESAFRepository(ABC):
    @abstractmethod
    def get(self, esaf_id: str) -> Optional[ESAFRecord]:
        """Return the ESAF with the given ID, or None if not found."""
        ...

    @abstractmethod
    def list(
        self,
        pi_group_slug: Optional[str] = None,
        beamline: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[ESAFRecord]:
        """Return all ESAFs, optionally filtered."""
        ...

    @abstractmethod
    def save(self, record: ESAFRecord) -> ESAFRecord:
        """Insert or update a record. Returns the saved record."""
        ...

    @abstractmethod
    def delete(self, esaf_id: str) -> bool:
        """Delete a record by ID. Returns True if deleted, False if not found."""
        ...

    @abstractmethod
    def save_pdf(self, esaf_id: str, data: bytes) -> bool:
        """Store the raw PDF bytes for an ESAF. Returns True on success."""
        ...

    @abstractmethod
    def get_pdf(self, esaf_id: str) -> Optional[bytes]:
        """Return the raw PDF bytes for an ESAF, or None if not stored."""
        ...


class PIGroupRepository(ABC):
    @abstractmethod
    def get(self, slug: str) -> Optional[PIGroup]:
        """Return the PI group with the given slug, or None if not found."""
        ...

    @abstractmethod
    def list(self) -> list[PIGroup]:
        """Return all PI groups."""
        ...

    @abstractmethod
    def save(self, group: PIGroup) -> PIGroup:
        """Insert or update a PI group. Returns the saved group."""
        ...

    @abstractmethod
    def delete(self, slug: str) -> bool:
        """Delete a PI group by slug. Returns True if deleted."""
        ...

    @abstractmethod
    def find_by_member(self, name: str) -> list[PIGroup]:
        """Return PI groups whose known_members list contains a fuzzy match for name."""
        ...


def get_repositories(config: dict) -> tuple[ESAFRepository, PIGroupRepository]:
    """Instantiate and return (ESAFRepository, PIGroupRepository) from config."""
    backend = config.get("backend", "sqlite")

    if backend == "mongodb":
        from .backends.mongo_backend import MongoESAFRepository, MongoPIGroupRepository

        uri = config["mongodb"]["uri"]
        db_name = config["mongodb"]["database"]
        return (
            MongoESAFRepository(uri=uri, db_name=db_name),
            MongoPIGroupRepository(uri=uri, db_name=db_name),
        )

    # Default: SQLite
    import os

    sqlite_cfg = config.get("sqlite", {})
    db_path = os.path.expanduser(
        sqlite_cfg.get("db_path", "~/.easy_bluesky/esaf_server/esaf.db")
    )
    pdf_dir = os.path.expanduser(
        sqlite_cfg.get("pdf_dir", "~/.easy_bluesky/esaf_server/pdfs/")
    )

    from .backends.sqlite_backend import SQLiteESAFRepository, SQLitePIGroupRepository

    return (
        SQLiteESAFRepository(db_path=db_path, pdf_dir=pdf_dir),
        SQLitePIGroupRepository(db_path=db_path),
    )

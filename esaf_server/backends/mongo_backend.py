"""MongoDB backend using pymongo. PDFs stored in GridFS."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional

from ..models import ESAFRecord, PIGroup
from ..repository import ESAFRepository, PIGroupRepository


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_doc(doc: dict) -> ESAFRecord:
    doc = dict(doc)
    doc.pop("_id", None)
    return ESAFRecord(**doc)


def _group_from_doc(doc: dict) -> PIGroup:
    doc = dict(doc)
    doc.pop("_id", None)
    return PIGroup(**doc)


class MongoESAFRepository(ESAFRepository):
    def __init__(self, uri: str, db_name: str):
        import pymongo
        import gridfs

        self._client = pymongo.MongoClient(uri)
        self._db = self._client[db_name]
        self._col = self._db["esafs"]
        self._fs = gridfs.GridFS(self._db, collection="esaf_pdfs")

        # Index on esaf_id for fast lookups (esaf_id is already used as _id alternative)
        self._col.create_index("esaf_id", unique=True)

    # ------------------------------------------------------------------
    def get(self, esaf_id: str) -> Optional[ESAFRecord]:
        doc = self._col.find_one({"esaf_id": esaf_id})
        if doc is None:
            return None
        return _record_from_doc(doc)

    def list(
        self,
        pi_group_slug: Optional[str] = None,
        beamline: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[ESAFRecord]:
        query: dict = {}
        if pi_group_slug:
            query["pi_group_slug"] = pi_group_slug
        if beamline:
            query["beamline"] = {"$regex": f"^{beamline}$", "$options": "i"}
        if search:
            query["$or"] = [
                {"esaf_id": {"$regex": search, "$options": "i"}},
                {"title": {"$regex": search, "$options": "i"}},
                {"beamline": {"$regex": search, "$options": "i"}},
                {"pi_group_slug": {"$regex": search, "$options": "i"}},
                {"proposal_id": {"$regex": search, "$options": "i"}},
            ]

        docs = list(self._col.find(query).sort("updated_at", -1))
        records: list[ESAFRecord] = []
        for doc in docs:
            try:
                records.append(_record_from_doc(doc))
            except Exception:
                continue
        return records

    def save(self, record: ESAFRecord) -> ESAFRecord:
        now = _now_iso()
        existing = self.get(record.esaf_id)
        if existing is None:
            if not record.created_at:
                record = record.model_copy(update={"created_at": now})
        record = record.model_copy(update={"updated_at": now})
        doc = record.model_dump()
        self._col.replace_one(
            {"esaf_id": record.esaf_id},
            doc,
            upsert=True,
        )
        return record

    def delete(self, esaf_id: str) -> bool:
        result = self._col.delete_one({"esaf_id": esaf_id})
        return result.deleted_count > 0

    # ------------------------------------------------------------------
    def save_pdf(self, esaf_id: str, data: bytes) -> bool:
        try:
            # Remove old file if it exists
            for old in self._fs.find({"filename": esaf_id}):
                self._fs.delete(old._id)
            self._fs.put(data, filename=esaf_id)
            # Mark pdf_available on the record
            rec = self.get(esaf_id)
            if rec is not None:
                self.save(rec.model_copy(update={"pdf_available": True}))
            return True
        except Exception:
            return False

    def get_pdf(self, esaf_id: str) -> Optional[bytes]:
        try:
            grid_out = self._fs.find_one({"filename": esaf_id})
            if grid_out is None:
                return None
            return grid_out.read()
        except Exception:
            return None


class MongoPIGroupRepository(PIGroupRepository):
    def __init__(self, uri: str, db_name: str):
        import pymongo

        self._client = pymongo.MongoClient(uri)
        self._db = self._client[db_name]
        self._col = self._db["pi_groups"]
        self._col.create_index("slug", unique=True)

    # ------------------------------------------------------------------
    def get(self, slug: str) -> Optional[PIGroup]:
        doc = self._col.find_one({"slug": slug})
        if doc is None:
            return None
        return _group_from_doc(doc)

    def list(self) -> list[PIGroup]:
        docs = list(self._col.find().sort("slug", 1))
        groups: list[PIGroup] = []
        for doc in docs:
            try:
                groups.append(_group_from_doc(doc))
            except Exception:
                continue
        return groups

    def save(self, group: PIGroup) -> PIGroup:
        now = _now_iso()
        existing = self.get(group.slug)
        if existing is None and not group.created_at:
            group = group.model_copy(update={"created_at": now})
        doc = group.model_dump()
        self._col.replace_one({"slug": group.slug}, doc, upsert=True)
        return group

    def delete(self, slug: str) -> bool:
        result = self._col.delete_one({"slug": slug})
        return result.deleted_count > 0

    def find_by_member(self, name: str) -> list[PIGroup]:
        """Case-insensitive substring match against known_members."""
        docs = list(
            self._col.find(
                {"known_members": {"$regex": name, "$options": "i"}}
            ).sort("slug", 1)
        )
        groups: list[PIGroup] = []
        for doc in docs:
            try:
                groups.append(_group_from_doc(doc))
            except Exception:
                continue
        return groups

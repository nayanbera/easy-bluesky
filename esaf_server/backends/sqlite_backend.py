"""SQLite backend using stdlib sqlite3. Records stored as JSON blobs. PDFs on disk."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from ..models import ESAFRecord, PIGroup
from ..repository import ESAFRepository, PIGroupRepository


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_row(row: sqlite3.Row) -> ESAFRecord:
    data = json.loads(row["data"])
    return ESAFRecord(**data)


def _group_from_row(row: sqlite3.Row) -> PIGroup:
    data = json.loads(row["data"])
    return PIGroup(**data)


class SQLiteESAFRepository(ESAFRepository):
    def __init__(self, db_path: str, pdf_dir: str):
        self._db_path = db_path
        self._pdf_dir = pdf_dir
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(pdf_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS esafs (
                    esaf_id   TEXT PRIMARY KEY,
                    data      TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    def get(self, esaf_id: str) -> Optional[ESAFRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM esafs WHERE esaf_id = ?", (esaf_id,)
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def list(
        self,
        pi_group_slug: Optional[str] = None,
        beamline: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[ESAFRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM esafs ORDER BY updated_at DESC").fetchall()

        records: list[ESAFRecord] = []
        for row in rows:
            try:
                rec = _record_from_row(row)
            except Exception:
                continue

            if pi_group_slug and rec.pi_group_slug != pi_group_slug:
                continue
            if beamline and rec.beamline.lower() != beamline.lower():
                continue
            if search:
                needle = search.lower()
                haystack = " ".join([
                    rec.esaf_id, rec.title, rec.beamline,
                    rec.pi_group_slug, rec.proposal_id,
                ]).lower()
                if needle not in haystack:
                    continue
            records.append(rec)
        return records

    def save(self, record: ESAFRecord) -> ESAFRecord:
        now = _now_iso()
        existing = self.get(record.esaf_id)
        if existing is None:
            if not record.created_at:
                record = record.model_copy(update={"created_at": now})
        record = record.model_copy(update={"updated_at": now})
        data_json = record.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO esafs (esaf_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(esaf_id) DO UPDATE SET
                    data       = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (record.esaf_id, data_json, record.created_at, record.updated_at),
            )
            conn.commit()
        return record

    def delete(self, esaf_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM esafs WHERE esaf_id = ?", (esaf_id,))
            conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    def _pdf_path(self, esaf_id: str) -> str:
        safe = esaf_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._pdf_dir, f"{safe}.pdf")

    def save_pdf(self, esaf_id: str, data: bytes) -> bool:
        path = self._pdf_path(esaf_id)
        try:
            with open(path, "wb") as fh:
                fh.write(data)
            # Mark pdf_available on the record
            rec = self.get(esaf_id)
            if rec is not None:
                self.save(rec.model_copy(update={"pdf_available": True}))
            return True
        except OSError:
            return False

    def get_pdf(self, esaf_id: str) -> Optional[bytes]:
        path = self._pdf_path(esaf_id)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()


class SQLitePIGroupRepository(PIGroupRepository):
    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pi_groups (
                    slug       TEXT PRIMARY KEY,
                    data       TEXT NOT NULL,
                    created_at TEXT
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    def get(self, slug: str) -> Optional[PIGroup]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pi_groups WHERE slug = ?", (slug,)
            ).fetchone()
        if row is None:
            return None
        return _group_from_row(row)

    def list(self) -> list[PIGroup]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pi_groups ORDER BY slug"
            ).fetchall()
        groups: list[PIGroup] = []
        for row in rows:
            try:
                groups.append(_group_from_row(row))
            except Exception:
                continue
        return groups

    def save(self, group: PIGroup) -> PIGroup:
        now = _now_iso()
        existing = self.get(group.slug)
        if existing is None and not group.created_at:
            group = group.model_copy(update={"created_at": now})
        data_json = group.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pi_groups (slug, data, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET data = excluded.data
                """,
                (group.slug, data_json, group.created_at),
            )
            conn.commit()
        return group

    def delete(self, slug: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM pi_groups WHERE slug = ?", (slug,))
            conn.commit()
        return cursor.rowcount > 0

    def find_by_member(self, name: str) -> list[PIGroup]:
        needle = name.lower()
        results: list[PIGroup] = []
        for group in self.list():
            for member in group.known_members:
                if needle in member.lower():
                    results.append(group)
                    break
        return results

"""Pydantic v2 models shared between all backends and the API."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class ESAFUser(BaseModel):
    name: str
    institution: str = ""
    role: str = ""   # "PI", "co-investigator", "user"
    email: str = ""


class ESAFRecord(BaseModel):
    esaf_id: str
    title: str = ""
    start_date: str = ""       # YYYY-MM-DD
    end_date: str = ""
    beamline: str = ""
    proposal_id: str = ""
    pi_group_slug: str = ""    # assigned at import time, not from ESAF
    users: list[ESAFUser] = Field(default_factory=list)
    source: str = "manual"     # "pdf", "server", "manual"
    raw_fields: dict = Field(default_factory=dict)  # everything else extracted from PDF
    extra_fields: dict = Field(default_factory=dict)  # user-defined key-value pairs
    pdf_available: bool = False
    created_at: str = ""       # ISO datetime
    updated_at: str = ""


class PIGroup(BaseModel):
    slug: str                  # e.g. "uchicago_john_rogers"
    pi_first_name: str
    pi_last_name: str
    pi_institution: str
    univ_short_name: str       # e.g. "uchicago", "anl", "mit"
    known_members: list[str] = Field(default_factory=list)  # names appearing in ESAFs
    created_at: str = ""


class ESAFCreate(BaseModel):
    """ESAFRecord fields for creation — pdf_available/created_at/updated_at omitted."""
    esaf_id: str
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    beamline: str = ""
    proposal_id: str = ""
    pi_group_slug: str = ""
    users: list[ESAFUser] = Field(default_factory=list)
    source: str = "manual"
    raw_fields: dict = Field(default_factory=dict)
    extra_fields: dict = Field(default_factory=dict)


class ESAFPatch(BaseModel):
    """Partial update for extra_fields only.

    Keys map to new string values.  Set a key to None to remove it.
    """
    fields: dict


class PIGroupCreate(BaseModel):
    """PIGroup fields for creation — created_at omitted."""
    slug: str
    pi_first_name: str
    pi_last_name: str
    pi_institution: str
    univ_short_name: str
    known_members: list[str] = Field(default_factory=list)


class ParsedPDFResult(BaseModel):
    record: ESAFRecord
    confidence: dict[str, float]  # field -> confidence 0.0-1.0
    raw_text: str

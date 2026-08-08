"""PDF parser for APS ESAF documents using pdfplumber + regex."""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Union

from .models import ESAFRecord, ESAFUser, ParsedPDFResult

# ---------------------------------------------------------------------------
# Date normalisation helpers
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

# Regex patterns for dates
_RE_DATE_MDY = re.compile(
    r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})"
)
_RE_DATE_YMD = re.compile(
    r"(\d{4})[/\-](\d{2})[/\-](\d{2})"
)
_RE_DATE_NAMED = re.compile(
    r"([A-Za-z]+)\s+(\d{1,2})[,\s]+(\d{4})"
)


def _normalise_date(raw: str) -> str:
    """Convert various date formats to YYYY-MM-DD, or return raw if unrecognised."""
    raw = raw.strip()
    m = _RE_DATE_YMD.search(raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = _RE_DATE_MDY.search(raw)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    m = _RE_DATE_NAMED.search(raw)
    if m:
        month_str = m.group(1).lower()
        month_num = _MONTH_NAMES.get(month_str)
        if month_num:
            return f"{m.group(3)}-{str(month_num).zfill(2)}-{m.group(2).zfill(2)}"
    return raw


# ---------------------------------------------------------------------------
# Field extraction patterns
# ---------------------------------------------------------------------------

# Each entry: (field_name, [(regex_pattern, group_for_value, confidence)])
_FIELD_PATTERNS: list[tuple[str, list[tuple[str, int, float]]]] = [
    (
        "esaf_id",
        [
            (r"ESAF\s*(?:Number|ID|No\.?|#)\s*[:\-]?\s*(\d+)", 1, 1.0),
            (r"Experiment\s+Safety\s+Assessment\s+Form\s+(?:Number|No\.?|#)\s*[:\-]?\s*(\d+)", 1, 1.0),
            (r"ESAF\s+(\d{4,8})", 1, 0.7),
            (r"(?:^|\s)(\d{6,8})(?:\s|$)", 1, 0.4),  # bare 6-8 digit number
        ],
    ),
    (
        "title",
        [
            (r"(?:Experiment\s+)?Title\s*[:\-]\s*(.+?)(?:\n|$)", 1, 1.0),
            (r"(?:Experiment\s+)?Description\s*[:\-]\s*(.+?)(?:\n|$)", 1, 0.7),
        ],
    ),
    (
        "start_date",
        [
            (
                r"(?:Experiment\s+)?Start\s+Date\s*[:\-]\s*"
                r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{4}[/\-]\d{2}[/\-]\d{2}|[A-Za-z]+\s+\d{1,2}[,\s]+\d{4})",
                1, 1.0,
            ),
            (r"(?:Begin(?:ning)?\s+Date|From)\s*[:\-]\s*(\S+)", 1, 0.7),
        ],
    ),
    (
        "end_date",
        [
            (
                r"(?:Experiment\s+)?End\s+Date\s*[:\-]\s*"
                r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{4}[/\-]\d{2}[/\-]\d{2}|[A-Za-z]+\s+\d{1,2}[,\s]+\d{4})",
                1, 1.0,
            ),
            (r"(?:Finish\s+Date|Through|To)\s*[:\-]\s*(\S+)", 1, 0.7),
        ],
    ),
    (
        "beamline",
        [
            (r"Beamline\s*[:\-]\s*(.+?)(?:\n|$)", 1, 1.0),
            (r"Sector\s*[:\-]\s*(.+?)(?:\n|$)", 1, 0.6),
        ],
    ),
    (
        "proposal_id",
        [
            (r"Proposal\s+(?:Number|ID|No\.?)\s*[:\-]\s*(\S+)", 1, 1.0),
            (r"GUP[:\-\s]*(\d+)", 1, 0.9),
        ],
    ),
]

# Pattern to detect a user-table row (Name, Institution, Role)
# Flexible: captures 2-4 columns separated by whitespace/tabs
_RE_USER_ROW = re.compile(
    r"^([A-Z][a-z]+(?: [A-Z][a-zA-Z'\-]+){1,4})\s{2,}([^\t\n]{3,40})\s{2,}([^\t\n]{2,30})",
    re.MULTILINE,
)

_ROLE_KEYWORDS = {"pi", "principal investigator", "co-investigator", "user", "staff"}


def _extract_users(text: str) -> list[ESAFUser]:
    """Heuristically extract user rows from a text block."""
    users: list[ESAFUser] = []
    seen: set[str] = set()

    # Look for a "Users" or "Personnel" section
    section_match = re.search(
        r"(?:Users|Personnel|Participants|User\s+List)\s*[:\-]?\s*\n(.*?)(?:\n\n|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    block = section_match.group(1) if section_match else text

    for m in _RE_USER_ROW.finditer(block):
        name = m.group(1).strip()
        institution = m.group(2).strip()
        role_raw = m.group(3).strip()

        # Normalise role
        role_lower = role_raw.lower()
        if "principal" in role_lower or role_lower == "pi":
            role = "PI"
        elif "co" in role_lower and "invest" in role_lower:
            role = "co-investigator"
        elif "user" in role_lower:
            role = "user"
        else:
            role = role_raw

        if name not in seen:
            seen.add(name)
            users.append(ESAFUser(name=name, institution=institution, role=role))

    return users


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_esaf_pdf(pdf_path_or_bytes: Union[str, bytes]) -> ParsedPDFResult:
    """Extract ESAF fields from a PDF file or bytes.

    Returns a ParsedPDFResult with the extracted record, per-field confidence
    scores (0.0-1.0), and the full extracted text.
    """
    import pdfplumber

    # Open PDF
    if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
        pdf_file = io.BytesIO(pdf_path_or_bytes)
        pdf = pdfplumber.open(pdf_file)
    else:
        pdf = pdfplumber.open(pdf_path_or_bytes)

    try:
        pages_text: list[str] = []
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t)
        raw_text = "\n".join(pages_text)
    finally:
        pdf.close()

    # ----- Extract fields -----
    extracted: dict[str, str] = {}
    confidence: dict[str, float] = {}
    raw_fields: dict[str, str] = {}

    for field_name, patterns in _FIELD_PATTERNS:
        for pattern, group_idx, conf in patterns:
            m = re.search(pattern, raw_text, re.IGNORECASE | re.MULTILINE)
            if m:
                value = m.group(group_idx).strip()
                # Truncate long values (titles can be long, others shouldn't be)
                if field_name != "title":
                    value = value[:80]
                extracted[field_name] = value
                confidence[field_name] = conf
                raw_fields[field_name] = value
                break
        else:
            confidence[field_name] = 0.0

    # Normalise dates
    for date_field in ("start_date", "end_date"):
        if date_field in extracted:
            extracted[date_field] = _normalise_date(extracted[date_field])

    # Extract users
    users = _extract_users(raw_text)
    if users:
        confidence["users"] = 0.8
    else:
        confidence["users"] = 0.0

    # Build ESAFRecord
    from datetime import timezone
    now = datetime.now(timezone.utc).isoformat()
    record = ESAFRecord(
        esaf_id=extracted.get("esaf_id", "UNKNOWN"),
        title=extracted.get("title", ""),
        start_date=extracted.get("start_date", ""),
        end_date=extracted.get("end_date", ""),
        beamline=extracted.get("beamline", ""),
        proposal_id=extracted.get("proposal_id", ""),
        pi_group_slug="",
        users=users,
        source="pdf",
        raw_fields=raw_fields,
        pdf_available=True,
        created_at=now,
        updated_at=now,
    )

    return ParsedPDFResult(
        record=record,
        confidence=confidence,
        raw_text=raw_text,
    )

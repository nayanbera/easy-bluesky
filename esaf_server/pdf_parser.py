"""PDF parser for APS ESAF documents using pdfplumber + regex.

Tuned to the APS "Experiment Hazard Control Plan Report" PDF format
(the multi-page document produced by the APS ESAF system, e.g. ESAF-289784_report.pdf).

Key structural features of this format
---------------------------------------
Page-1 / repeated header (pages 1, 5, 6, 7):
    PEN: 15-IDCD-2026-17 ESAF ID: 289784 (GUP)
    ID Start Date: 02/06/2026 08:00 AM ID End Date: 02/17/2026 04:00 PM
    Spokesperson: Schlossman GUP ID: 1018531
    Title: Complexation of Lanthanides to LBT Peptide Surfactants at the Water Surface

Materials section header (page 1) — NOT the beamline name:
    Beamline Laboratory Used
    Start Date: 06-FEB-26 End Date: 17-FEB-26

Personnel training table (page 4):
    User First Last Name  Access ...
    Type Name  End Date ...
    On-site Erik Binter  02-SEP-26  OK  ...
    On-site Bikash Sapkota  25-MAR-27  OK  ...
    On-site Mark Schlossman  27-MAY-28  OK  ...

Authorization signature block (pages 6-7):
    Name  Institution  Signature  Date
    Bikash Sapkota  University of Illinois at Chicago  ____  ____
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Union

from .models import ESAFRecord, ESAFUser, ParsedPDFResult


# ---------------------------------------------------------------------------
# Month lookup
# ---------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

def _normalise_date(raw: str) -> str:
    """Convert APS date strings to YYYY-MM-DD.

    Handles:
      MM/DD/YYYY        02/06/2026
      YYYY-MM-DD        2026-02-06
      DD-MON-YYYY       06-FEB-2026
      DD-MON-YY         06-FEB-26   (year < 50 → 20xx, else 19xx)
      Month DD, YYYY    February 6, 2026
    """
    raw = raw.strip()

    # YYYY-MM-DD
    m = re.search(r"(\d{4})[/\-](\d{2})[/\-](\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # MM/DD/YYYY  or  M/D/YYYY
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    # DD-MON-YYYY  or  DD-MON-YY
    m = re.search(r"(\d{1,2})-([A-Za-z]{3,})-(\d{2,4})", raw)
    if m:
        month_num = _MONTHS.get(m.group(2).lower())
        if month_num:
            year = int(m.group(3))
            if year < 100:
                year += 2000 if year < 50 else 1900
            return f"{year:04d}-{month_num:02d}-{int(m.group(1)):02d}"

    # Month DD, YYYY  or  Month DD YYYY
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2})[,\s]+(\d{4})", raw)
    if m:
        month_num = _MONTHS.get(m.group(1).lower())
        if month_num:
            return f"{m.group(3)}-{month_num:02d}-{int(m.group(2)):02d}"

    return raw


# ---------------------------------------------------------------------------
# PEN → beamline name
# ---------------------------------------------------------------------------

def _pen_to_beamline(pen: str) -> str:
    """Convert an APS PEN like '15-IDCD-2026-17' to a beamline name '15-ID-CD'.

    APS PEN format: <sector>-<branch><endstation>-<year>-<number>
    Examples:
      15-IDCD-2026-17  →  15-ID-CD
      15-IDB-2025-5    →  15-ID-B
      12-BM-2026-3     →  12-BM
    """
    m = re.match(r"(\d+)-([A-Z\d]+)-\d{4}", pen.strip())
    if not m:
        return pen
    sector, branch = m.group(1), m.group(2)
    # Known insertion-device / bending-magnet prefixes
    for prefix in ("ID", "BM", "XSD", "XFD", "LOM", "EXP"):
        if branch.startswith(prefix) and len(branch) > len(prefix):
            return f"{sector}-{prefix}-{branch[len(prefix):]}"
    return f"{sector}-{branch}"


# ---------------------------------------------------------------------------
# User extraction
# ---------------------------------------------------------------------------

def _extract_users(text: str, spokesperson_last: str = "") -> list[ESAFUser]:
    """Extract users from APS ESAF text.

    Strategy (in order):
    1.  Parse the "Experiment Personnel and Training Due Dates" table:
          On-site  Erik Binter  02-SEP-26  OK  ...
          Remote   Alice Smith  01-JAN-27  OK  ...
    2.  If (1) found nothing, fall back to the Authorization signature block:
          Bikash Sapkota  University of Illinois at Chicago  ____  ____
    3.  Attach institutions by cross-matching names to the signature block.
    4.  Mark the spokesperson (PI) by last-name match.
    """
    users: list[ESAFUser] = []
    seen: set[str] = set()

    # --- Step 1: personnel training table ---
    for m in re.finditer(
        r"^(On-site|Remote)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z'\-\.]+)+)"   # First [Middle] Last
        r"\s+\d{2}-[A-Z]+-\d{2,4}",                    # access end date
        text,
        re.MULTILINE,
    ):
        name = m.group(2).strip()
        if name not in seen:
            seen.add(name)
            users.append(ESAFUser(name=name, institution="", role=""))

    # --- Step 2: fallback — Authorization signature block ---
    if not users:
        for m in re.finditer(
            r"^([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z'\-\.]+)+)"  # Name
            r" +(.+?) +_{4,}",                              # Institution  _____
            text,
            re.MULTILINE,
        ):
            name = m.group(1).strip()
            inst = m.group(2).strip()
            # Reject obvious non-names (all-caps, very short)
            if name not in seen and len(name) < 60:
                seen.add(name)
                users.append(ESAFUser(name=name, institution=inst, role=""))

    # --- Step 3: enrich institutions from Authorization signature block ---
    # Anchor each search to the known user name to avoid greedy-match errors
    # (institution words start with capitals, confusing a generic name pattern).
    for user in users:
        if user.institution:
            continue
        m = re.search(
            re.escape(user.name) + r"[ ]+([^\n]+?)[ ]+_{4,}",
            text, re.MULTILINE,
        )
        if m:
            inst = m.group(1).strip()
            if len(inst) > 3 and not inst.isupper():
                user.institution = inst

    # --- Step 4: mark PI by last-name match to spokesperson ---
    if spokesperson_last:
        last_lower = spokesperson_last.strip().lower()
        for user in users:
            parts = user.name.split()
            if parts and parts[-1].lower() == last_lower:
                user.role = "PI"
                break

    for user in users:
        if not user.role:
            user.role = "user"

    return users


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_esaf_pdf(pdf_path_or_bytes: Union[str, bytes]) -> ParsedPDFResult:
    """Extract ESAF fields from an APS ESAF PDF file or bytes."""
    import pdfplumber

    if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
        pdf = pdfplumber.open(io.BytesIO(pdf_path_or_bytes))
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

    confidence: dict[str, float] = {}
    raw_fields: dict[str, str] = {}
    extracted: dict[str, str] = {}

    # ── ESAF ID ───────────────────────────────────────────────────────────────
    # "ESAF ID: 289784 (GUP)"
    m = re.search(r"ESAF\s+ID:\s*(\d+)", raw_text)
    if m:
        extracted["esaf_id"] = m.group(1)
        confidence["esaf_id"] = 1.0
    else:
        m = re.search(r"ESAF\s*(?:Number|No\.?|#)\s*[:\-]?\s*(\d+)", raw_text, re.IGNORECASE)
        if m:
            extracted["esaf_id"] = m.group(1)
            confidence["esaf_id"] = 0.9
        else:
            confidence["esaf_id"] = 0.0

    # ── Title ─────────────────────────────────────────────────────────────────
    # "Title: Complexation of Lanthanides ..."
    m = re.search(r"^Title:\s*(.+)$", raw_text, re.MULTILINE)
    if m:
        extracted["title"] = m.group(1).strip()
        confidence["title"] = 1.0
    else:
        m = re.search(r"(?:Experiment\s+)?Title\s*[:\-]\s*(.+?)(?:\n|$)", raw_text, re.IGNORECASE)
        if m:
            extracted["title"] = m.group(1).strip()
            confidence["title"] = 0.8
        else:
            confidence["title"] = 0.0

    # ── Dates ─────────────────────────────────────────────────────────────────
    # Primary: "ID Start Date: 02/06/2026 08:00 AM"  (full 4-digit year, reliable)
    m = re.search(r"ID\s+Start\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", raw_text)
    if m:
        extracted["start_date"] = _normalise_date(m.group(1))
        confidence["start_date"] = 1.0
    else:
        # Secondary: "Start Date: 06-FEB-26"  (2-digit year)
        m = re.search(r"(?<![A-Za-z])Start\s+Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})", raw_text)
        if m:
            extracted["start_date"] = _normalise_date(m.group(1))
            confidence["start_date"] = 0.7
        else:
            m = re.search(
                r"Start\s+Date\s*[:\-]\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|[A-Za-z]+\s+\d{1,2}[,\s]+\d{4})",
                raw_text, re.IGNORECASE,
            )
            if m:
                extracted["start_date"] = _normalise_date(m.group(1))
                confidence["start_date"] = 0.6
            else:
                confidence["start_date"] = 0.0

    m = re.search(r"ID\s+End\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", raw_text)
    if m:
        extracted["end_date"] = _normalise_date(m.group(1))
        confidence["end_date"] = 1.0
    else:
        m = re.search(r"(?<![A-Za-z])End\s+Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})", raw_text)
        if m:
            extracted["end_date"] = _normalise_date(m.group(1))
            confidence["end_date"] = 0.7
        else:
            m = re.search(
                r"End\s+Date\s*[:\-]\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|[A-Za-z]+\s+\d{1,2}[,\s]+\d{4})",
                raw_text, re.IGNORECASE,
            )
            if m:
                extracted["end_date"] = _normalise_date(m.group(1))
                confidence["end_date"] = 0.6
            else:
                confidence["end_date"] = 0.0

    # ── Beamline — derive from PEN, not "Beamline Laboratory Used" ───────────
    # "PEN: 15-IDCD-2026-17"  →  "15-ID-CD"
    m = re.search(r"PEN:\s*([\w\-]+)", raw_text)
    if m:
        pen = m.group(1).strip()
        raw_fields["pen"] = pen
        extracted["beamline"] = _pen_to_beamline(pen)
        confidence["beamline"] = 0.9
    else:
        # Fallback: "Beamline: X-ID-B" — skip "Laboratory Used"
        m = re.search(r"Beamline\s*[:\-]\s*(.+?)(?:\n|$)", raw_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if "laboratory" not in val.lower() and len(val) < 30:
                extracted["beamline"] = val
                confidence["beamline"] = 0.7
            else:
                confidence["beamline"] = 0.0
        else:
            m = re.search(r"Sector\s*[:\-]\s*(.+?)(?:\n|$)", raw_text, re.IGNORECASE)
            if m:
                extracted["beamline"] = m.group(1).strip()
                confidence["beamline"] = 0.5
            else:
                confidence["beamline"] = 0.0

    # ── Proposal / GUP ID ────────────────────────────────────────────────────
    # "GUP ID: 1018531"
    m = re.search(r"GUP\s+ID:\s*(\d+)", raw_text)
    if m:
        extracted["proposal_id"] = f"GUP-{m.group(1)}"
        confidence["proposal_id"] = 1.0
    else:
        # "GUP-1018531" or "GUP: 1018531" or bare "GUP 1018531"
        m = re.search(r"GUP\s*[:\-]?\s*(\d{4,})", raw_text)
        if m:
            extracted["proposal_id"] = f"GUP-{m.group(1)}"
            confidence["proposal_id"] = 0.9
        else:
            m = re.search(r"Proposal\s+(?:Number|ID|No\.?)\s*[:\-]\s*(\S+)", raw_text, re.IGNORECASE)
            if m:
                extracted["proposal_id"] = m.group(1)
                confidence["proposal_id"] = 0.7
            else:
                confidence["proposal_id"] = 0.0

    # ── Spokesperson — used to tag the PI in the user list ───────────────────
    spokesperson_last = ""
    m = re.search(r"Spokesperson:\s*(\S+)", raw_text)
    if m:
        spokesperson_last = m.group(1).strip()
        raw_fields["spokesperson_last"] = spokesperson_last

    # ── Users ─────────────────────────────────────────────────────────────────
    users = _extract_users(raw_text, spokesperson_last)
    confidence["users"] = 0.9 if users else 0.0

    # Keep all extracted values in raw_fields for transparency
    for k, v in extracted.items():
        raw_fields.setdefault(k, v)

    # ── Build record ──────────────────────────────────────────────────────────
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

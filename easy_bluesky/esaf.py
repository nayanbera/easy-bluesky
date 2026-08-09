"""esaf.py — ESAF data layer: dataclasses, local cache, PI Group registry, HTTP client, PDF parser.

No Qt imports — pure Python.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Exception ──────────────────────────────────────────────────────────────────

class ESAFServerError(Exception):
    """Raised when the ESAF HTTP server returns an error."""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ESAFUser:
    name: str
    institution: str = ""
    role: str = ""
    email: str = ""


@dataclass
class ESAFRecord:
    esaf_id: str
    title: str = ""
    start_date: str = ""    # YYYY-MM-DD
    end_date: str = ""
    beamline: str = ""
    proposal_id: str = ""
    pi_group_slug: str = ""
    users: list = field(default_factory=list)   # list of ESAFUser
    source: str = "manual"   # "pdf", "server", "manual"
    raw_fields: dict = field(default_factory=dict)
    extra_fields: dict = field(default_factory=dict)   # user-defined key-value pairs
    pdf_available: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass
class PIGroup:
    slug: str                  # e.g. "uchicago_john_rogers"
    pi_first_name: str
    pi_last_name: str
    pi_institution: str
    univ_short_name: str       # e.g. "uchicago", "anl", "mit"
    known_members: list = field(default_factory=list)   # list of str
    created_at: str = ""

    @property
    def pi_name(self) -> str:
        return f"{self.pi_last_name}, {self.pi_first_name}"

    @property
    def display_name(self) -> str:
        return f"{self.pi_name}  ({self.univ_short_name.upper()})"


# ── Slug generation ────────────────────────────────────────────────────────────

def make_pi_slug(univ_short: str, first: str, last: str) -> str:
    """Generate slug like uchicago_john_rogers from components.

    Lowercases, strips punctuation, replaces spaces with underscores.
    """
    def _clean(s: str) -> str:
        # Normalize unicode → ASCII equivalents where possible
        s = unicodedata.normalize("NFKD", s)
        s = s.encode("ascii", "ignore").decode("ascii")
        s = s.lower().strip()
        # Spaces and hyphens → underscore
        s = re.sub(r"[\s\-]+", "_", s)
        # Remove any remaining non-alphanumeric/underscore chars
        s = re.sub(r"[^a-z0-9_]", "", s)
        return s

    parts = [_clean(univ_short), _clean(first), _clean(last)]
    slug = "_".join(p for p in parts if p)
    return slug or "unknown_group"


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _user_to_dict(u: ESAFUser) -> dict:
    return {
        "name": u.name,
        "institution": u.institution,
        "role": u.role,
        "email": u.email,
    }


def _user_from_dict(d: dict) -> ESAFUser:
    return ESAFUser(
        name=d.get("name", ""),
        institution=d.get("institution", ""),
        role=d.get("role", ""),
        email=d.get("email", ""),
    )


def _record_to_dict(r: ESAFRecord) -> dict:
    return {
        "esaf_id": r.esaf_id,
        "title": r.title,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "beamline": r.beamline,
        "proposal_id": r.proposal_id,
        "pi_group_slug": r.pi_group_slug,
        "users": [_user_to_dict(u) for u in r.users],
        "source": r.source,
        "raw_fields": r.raw_fields,
        "extra_fields": r.extra_fields,
        "pdf_available": r.pdf_available,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _record_from_dict(d: dict) -> ESAFRecord:
    return ESAFRecord(
        esaf_id=d.get("esaf_id", ""),
        title=d.get("title", ""),
        start_date=d.get("start_date", ""),
        end_date=d.get("end_date", ""),
        beamline=d.get("beamline", ""),
        proposal_id=d.get("proposal_id", ""),
        pi_group_slug=d.get("pi_group_slug", ""),
        users=[_user_from_dict(u) for u in d.get("users", [])],
        source=d.get("source", "manual"),
        raw_fields=d.get("raw_fields", {}),
        extra_fields=d.get("extra_fields", {}),
        pdf_available=d.get("pdf_available", False),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
    )


def _group_to_dict(g: PIGroup) -> dict:
    return {
        "slug": g.slug,
        "pi_first_name": g.pi_first_name,
        "pi_last_name": g.pi_last_name,
        "pi_institution": g.pi_institution,
        "univ_short_name": g.univ_short_name,
        "known_members": list(g.known_members),
        "created_at": g.created_at,
    }


def _group_from_dict(d: dict) -> PIGroup:
    return PIGroup(
        slug=d.get("slug", ""),
        pi_first_name=d.get("pi_first_name", ""),
        pi_last_name=d.get("pi_last_name", ""),
        pi_institution=d.get("pi_institution", ""),
        univ_short_name=d.get("univ_short_name", ""),
        known_members=list(d.get("known_members", [])),
        created_at=d.get("created_at", ""),
    )


# ── Local cache ────────────────────────────────────────────────────────────────

_CACHE_DIR = Path.home() / ".easy_bluesky" / "esaf_cache"


def save_cached(record: ESAFRecord):
    """Save an ESAFRecord to the local cache as <esaf_id>.json."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    if not record.created_at:
        record.created_at = now
    record.updated_at = now
    path = _CACHE_DIR / f"{record.esaf_id}.json"
    path.write_text(json.dumps(_record_to_dict(record), indent=2), encoding="utf-8")


def load_cached(esaf_id: str) -> ESAFRecord | None:
    """Load an ESAFRecord from the local cache. Returns None if not found or corrupt."""
    path = _CACHE_DIR / f"{esaf_id}.json"
    if not path.exists():
        return None
    try:
        return _record_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def list_cached() -> list[ESAFRecord]:
    """Return all ESAFRecords from the local cache (all *.json files)."""
    if not _CACHE_DIR.exists():
        return []
    records = []
    for p in sorted(_CACHE_DIR.glob("*.json")):
        try:
            records.append(_record_from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            pass
    return records


def delete_cached(esaf_id: str):
    """Delete a cached ESAF record by ID."""
    path = _CACHE_DIR / f"{esaf_id}.json"
    if path.exists():
        path.unlink()


# ── PI Group registry ──────────────────────────────────────────────────────────

class PIGroupRegistry:
    _path  = Path.home() / ".easy_bluesky" / "pi_groups.json"
    _cache: list | None = None   # in-memory cache; invalidated by save()

    @classmethod
    def load(cls) -> list[PIGroup]:
        """Return all PIGroups from the local registry file (cached after first read)."""
        if cls._cache is not None:
            return cls._cache
        if not cls._path.exists():
            cls._cache = []
            return cls._cache
        try:
            data = json.loads(cls._path.read_text(encoding="utf-8"))
            cls._cache = [_group_from_dict(d) for d in data]
        except Exception:
            cls._cache = []
        return cls._cache

    @classmethod
    def save(cls, groups: list[PIGroup]):
        """Persist the full list of PIGroups and invalidate the in-memory cache."""
        cls._path.parent.mkdir(parents=True, exist_ok=True)
        cls._path.write_text(
            json.dumps([_group_to_dict(g) for g in groups], indent=2),
            encoding="utf-8",
        )
        cls._cache = None   # force reload on next access

    @classmethod
    def get(cls, slug: str) -> PIGroup | None:
        """Return a PIGroup by slug, or None if not found."""
        for g in cls.load():
            if g.slug == slug:
                return g
        return None

    @classmethod
    def find_by_member(cls, name: str) -> list[PIGroup]:
        """Case-insensitive partial match against known_members."""
        name_lower = name.strip().lower()
        if not name_lower:
            return []
        results = []
        for g in cls.load():
            for member in g.known_members:
                if name_lower in member.lower():
                    results.append(g)
                    break
        return results

    @classmethod
    def add_or_update(cls, group: PIGroup):
        """Add a new PIGroup or replace an existing one with the same slug."""
        groups = cls.load()
        for i, g in enumerate(groups):
            if g.slug == group.slug:
                groups[i] = group
                cls.save(groups)
                return
        if not group.created_at:
            group.created_at = datetime.now(timezone.utc).isoformat()
        groups.append(group)
        cls.save(groups)

    @classmethod
    def delete(cls, slug: str):
        """Remove a PIGroup by slug. No-op if the slug does not exist."""
        groups = [g for g in cls.load() if g.slug != slug]
        cls.save(groups)


# ── HTTP client ────────────────────────────────────────────────────────────────

class ESAFServerClient:
    """Thin HTTP client for the optional ESAF server.

    Uses urllib.request (stdlib) — no external HTTP dependency.
    """

    def __init__(self, server_url: str, api_key: str = ""):
        self._base = server_url.rstrip("/")
        self._key = api_key

    def _headers(self, write: bool = False) -> dict:
        h = {"Content-Type": "application/json"}
        if write and self._key:
            h["X-API-Key"] = self._key
        return h

    def _request(self, method: str, path: str, data: bytes = None,
                 write: bool = False) -> dict:
        url = self._base + path
        headers = self._headers(write=write)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ESAFServerError(e.code, body or e.reason) from e
        except urllib.error.URLError as e:
            raise ESAFServerError(0, str(e.reason)) from e

    def _request_raw(self, method: str, path: str, data: bytes = None,
                     content_type: str = "application/json",
                     write: bool = False) -> bytes:
        """Make a raw request and return raw bytes response."""
        url = self._base + path
        headers = {"Content-Type": content_type}
        if write and self._key:
            headers["X-API-Key"] = self._key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ESAFServerError(e.code, body or e.reason) from e
        except urllib.error.URLError as e:
            raise ESAFServerError(0, str(e.reason)) from e

    def health(self) -> dict:
        """Return the server health dict {"status": ..., "backend": ...}.

        Raises ESAFServerError if the server is unreachable.
        """
        return self._request("GET", "/health")

    def list_esafs(self, **filters) -> list[ESAFRecord]:
        """List ESAFs from server, optionally filtered by keyword args."""
        path = "/esafs"
        if filters:
            params = urllib.parse.urlencode(filters)
            path += f"?{params}"
        try:
            data = self._request("GET", path)
            return [_record_from_dict(d) for d in data]
        except ESAFServerError:
            return []

    def get_esaf(self, esaf_id: str) -> ESAFRecord | None:
        """Fetch a single ESAF by ID from the server. Returns None on error."""
        try:
            data = self._request("GET", f"/esafs/{urllib.parse.quote(esaf_id)}")
            return _record_from_dict(data)
        except ESAFServerError:
            return None

    def create_esaf(self, record: ESAFRecord) -> ESAFRecord:
        """Create a new ESAF on the server. Raises ESAFServerError on failure."""
        body = json.dumps(_record_to_dict(record)).encode("utf-8")
        data = self._request("POST", "/esafs", data=body, write=True)
        return _record_from_dict(data)

    def update_esaf(self, record: ESAFRecord) -> ESAFRecord:
        """Update an existing ESAF on the server. Raises ESAFServerError on failure."""
        body = json.dumps(_record_to_dict(record)).encode("utf-8")
        data = self._request(
            "PUT", f"/esafs/{urllib.parse.quote(record.esaf_id)}", data=body, write=True
        )
        return _record_from_dict(data)

    def patch_extra_fields(self, esaf_id: str, fields: dict) -> ESAFRecord:
        """Merge ``fields`` into the ESAF's extra_fields on the server.

        Keys with value ``None`` are deleted from extra_fields on the server.
        Returns the updated ESAFRecord.  Raises ESAFServerError on failure.
        """
        body = json.dumps({"fields": fields}).encode("utf-8")
        data = self._request(
            "PATCH",
            f"/esafs/{urllib.parse.quote(esaf_id)}/extra_fields",
            data=body,
            write=True,
        )
        return _record_from_dict(data)

    def get_pdf(self, esaf_id: str) -> bytes | None:
        """Download ESAF PDF bytes from the server. Returns None on error."""
        try:
            return self._request_raw("GET", f"/esafs/{urllib.parse.quote(esaf_id)}/pdf")
        except ESAFServerError:
            return None

    def upload_pdf(self, esaf_id: str, pdf_bytes: bytes) -> bool:
        """Upload a PDF to the server. Returns True on success."""
        try:
            self._request_raw(
                "POST",
                f"/esafs/{urllib.parse.quote(esaf_id)}/pdf",
                data=pdf_bytes,
                content_type="application/pdf",
                write=True,
            )
            return True
        except ESAFServerError:
            return False

    def parse_pdf(self, pdf_bytes: bytes) -> tuple[ESAFRecord, dict]:
        """Send PDF to server for parsing. Returns (record, confidence_dict)."""
        raw = self._request_raw(
            "POST", "/esafs/parse-pdf",
            data=pdf_bytes, content_type="application/pdf", write=True,
        )
        result = json.loads(raw.decode("utf-8"))
        record = _record_from_dict(result.get("record", {}))
        confidence = result.get("confidence", {})
        return record, confidence

    def list_pi_groups(self) -> list[PIGroup]:
        """List all PI groups from the server."""
        try:
            data = self._request("GET", "/pi-groups")
            return [_group_from_dict(d) for d in data]
        except ESAFServerError:
            return []

    def get_pi_group(self, slug: str) -> PIGroup | None:
        """Fetch a single PI group by slug. Returns None on error."""
        try:
            data = self._request("GET", f"/pi-groups/{urllib.parse.quote(slug)}")
            return _group_from_dict(data)
        except ESAFServerError:
            return None

    def create_pi_group(self, group: PIGroup) -> PIGroup:
        """Create a new PI group on the server."""
        body = json.dumps(_group_to_dict(group)).encode("utf-8")
        data = self._request("POST", "/pi-groups", data=body, write=True)
        return _group_from_dict(data)

    def update_pi_group(self, group: PIGroup) -> PIGroup:
        """Update an existing PI group on the server."""
        body = json.dumps(_group_to_dict(group)).encode("utf-8")
        data = self._request(
            "PUT", f"/pi-groups/{urllib.parse.quote(group.slug)}", data=body, write=True
        )
        return _group_from_dict(data)

    def match_pi_group(self, member_name: str) -> list[PIGroup]:
        """Find PI groups whose known_members match the given name."""
        try:
            q = urllib.parse.quote(member_name)
            data = self._request("GET", f"/pi-groups/match?name={q}")
            return [_group_from_dict(d) for d in data]
        except ESAFServerError:
            return []


# ── Local PDF parser ───────────────────────────────────────────────────────────

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(raw: str) -> str:
    """Parse various APS date string formats into YYYY-MM-DD. Returns '' on failure."""
    raw = raw.strip()
    if not raw:
        return ""
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return raw
    # MM/DD/YYYY or M/D/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    # Month D YYYY or Month DD, YYYY
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", raw)
    if m:
        mname = m.group(1).lower()
        if mname in _MONTHS:
            return f"{m.group(3)}-{_MONTHS[mname]:02d}-{int(m.group(2)):02d}"
    # DD-Mon-YYYY  or  DD-Mon-YY  (APS uses 2-digit year: 06-FEB-26 = 2026-02-06)
    m = re.match(r"^(\d{1,2})-([A-Za-z]+)-(\d{2,4})$", raw)
    if m:
        mname = m.group(2).lower()
        if mname in _MONTHS:
            year = int(m.group(3))
            if year < 100:
                year += 2000 if year < 50 else 1900
            return f"{year:04d}-{_MONTHS[mname]:02d}-{int(m.group(1)):02d}"
    return ""


def _extract_first_date(text: str) -> str:
    """Find and parse the first date-like substring in text."""
    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text)
    if m:
        return _parse_date(m.group(0))
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return _parse_date(m.group(0))
    # DD-Mon-YYYY or DD-Mon-YY
    m = re.search(r"\d{1,2}-[A-Za-z]+-\d{2,4}", text)
    if m:
        return _parse_date(m.group(0))
    return ""


def _pen_to_beamline(pen: str) -> str:
    """Convert an APS PEN string like '15-IDCD-2026-17' to '15-ID-CD'."""
    m = re.match(r"(\d+)-([A-Z\d]+)-\d{4}", pen.strip())
    if not m:
        return pen
    sector, branch = m.group(1), m.group(2)
    for prefix in ("ID", "BM", "XSD", "XFD", "LOM", "EXP"):
        if branch.startswith(prefix) and len(branch) > len(prefix):
            return f"{sector}-{prefix}-{branch[len(prefix):]}"
    return f"{sector}-{branch}"


def parse_esaf_pdf(path_or_bytes) -> tuple[ESAFRecord, dict]:
    """Parse an APS ESAF PDF locally using pdfplumber.

    Returns (ESAFRecord, confidence_dict).
    Confidence: 1.0 = high-confidence regex match, 0.0 = not found.

    Handles the APS "Experiment Hazard Control Plan Report" format:
    - ESAF ID from "ESAF ID: NNNNNN" header line
    - Title from "Title: ..." header line
    - Start/End dates from "ID Start/End Date: MM/DD/YYYY" header
    - Beamline derived from "PEN: XX-BRANCH-YYYY-N" (not "Beamline Laboratory Used")
    - Proposal ID from "GUP ID: NNNNNNN" header
    - Users from "On-site / Remote" personnel training table
    - PI identified via "Spokesperson: LastName" header
    """
    import io as _io
    import pdfplumber

    if isinstance(path_or_bytes, (str, Path)):
        pdf = pdfplumber.open(str(path_or_bytes))
    else:
        pdf = pdfplumber.open(_io.BytesIO(path_or_bytes))

    pages_text = []
    with pdf as _pdf:
        for page in _pdf.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t)
    full_text = "\n".join(pages_text)

    raw_fields: dict = {}
    confidence: dict = {}
    extracted: dict = {}

    # ── ESAF ID ───────────────────────────────────────────────────────────────
    # "ESAF ID: 289784 (GUP)"
    m = re.search(r"ESAF\s+ID:\s*(\d+)", full_text)
    if m:
        extracted["esaf_id"] = m.group(1)
        confidence["esaf_id"] = 1.0
    else:
        m = re.search(r"ESAF\s*(?:Number|No\.?|#)\s*[:\-]?\s*(\d+)", full_text, re.IGNORECASE)
        if m:
            extracted["esaf_id"] = m.group(1)
            confidence["esaf_id"] = 0.9
        else:
            confidence["esaf_id"] = 0.0

    # ── Title ─────────────────────────────────────────────────────────────────
    # "Title: Complexation of ..."
    m = re.search(r"^Title:\s*(.+)$", full_text, re.MULTILINE)
    if m:
        extracted["title"] = m.group(1).strip()
        confidence["title"] = 1.0
    else:
        m = re.search(r"(?:Experiment\s+)?Title\s*[:\-]\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE)
        if m:
            extracted["title"] = m.group(1).strip()
            confidence["title"] = 0.8
        else:
            confidence["title"] = 0.0

    # ── Dates ─────────────────────────────────────────────────────────────────
    # Primary: "ID Start Date: 02/06/2026 08:00 AM"
    m = re.search(r"ID\s+Start\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text)
    if m:
        extracted["start_date"] = _parse_date(m.group(1))
        confidence["start_date"] = 1.0
    else:
        # Secondary: "Start Date: 06-FEB-26"  (2-digit year)
        m = re.search(r"(?<![A-Za-z])Start\s+Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})", full_text)
        if m:
            extracted["start_date"] = _parse_date(m.group(1))
            confidence["start_date"] = 0.7
        else:
            raw, _ = "", 0.0
            m = re.search(
                r"Start\s+Date\s*[:\-]?\s*([\d/\-A-Za-z,\s]+?)(?:\n|End|$)",
                full_text, re.IGNORECASE,
            )
            if m:
                d = _parse_date(m.group(1).strip()) or _extract_first_date(m.group(1))
                if d:
                    extracted["start_date"] = d
                    confidence["start_date"] = 0.6
                else:
                    confidence["start_date"] = 0.0
            else:
                confidence["start_date"] = 0.0

    m = re.search(r"ID\s+End\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text)
    if m:
        extracted["end_date"] = _parse_date(m.group(1))
        confidence["end_date"] = 1.0
    else:
        m = re.search(r"(?<![A-Za-z])End\s+Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{2,4})", full_text)
        if m:
            extracted["end_date"] = _parse_date(m.group(1))
            confidence["end_date"] = 0.7
        else:
            m = re.search(
                r"End\s+Date\s*[:\-]?\s*([\d/\-A-Za-z,\s]+?)(?:\n|$)",
                full_text, re.IGNORECASE,
            )
            if m:
                d = _parse_date(m.group(1).strip()) or _extract_first_date(m.group(1))
                if d:
                    extracted["end_date"] = d
                    confidence["end_date"] = 0.6
                else:
                    confidence["end_date"] = 0.0
            else:
                confidence["end_date"] = 0.0

    # ── Beamline — from PEN, not "Beamline Laboratory Used" ──────────────────
    # "PEN: 15-IDCD-2026-17"  →  "15-ID-CD"
    m = re.search(r"PEN:\s*([\w\-]+)", full_text)
    if m:
        pen = m.group(1).strip()
        raw_fields["pen"] = pen
        extracted["beamline"] = _pen_to_beamline(pen)
        confidence["beamline"] = 0.9
    else:
        m = re.search(r"Beamline\s*[:\-]?\s*([A-Za-z0-9\-\s]+?)(?:\n|$)", full_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if "laboratory" not in val.lower() and len(val) < 30:
                extracted["beamline"] = val
                confidence["beamline"] = 0.7
            else:
                confidence["beamline"] = 0.0
        else:
            confidence["beamline"] = 0.0

    # ── Proposal / GUP ID ────────────────────────────────────────────────────
    # "GUP ID: 1018531"
    m = re.search(r"GUP\s+ID:\s*(\d+)", full_text)
    if m:
        extracted["proposal_id"] = f"GUP-{m.group(1)}"
        confidence["proposal_id"] = 1.0
    else:
        m = re.search(r"GUP\s*[:\-]?\s*(\d{4,})", full_text)
        if m:
            extracted["proposal_id"] = f"GUP-{m.group(1)}"
            confidence["proposal_id"] = 0.9
        else:
            m = re.search(r"Proposal\s+(?:Number|ID|#)\s*[:\-]?\s*(\w[\w\-]*)", full_text, re.IGNORECASE)
            if m:
                extracted["proposal_id"] = m.group(1)
                confidence["proposal_id"] = 0.7
            else:
                confidence["proposal_id"] = 0.0

    # ── Spokesperson → PI ─────────────────────────────────────────────────────
    spokesperson_last = ""
    m = re.search(r"Spokesperson:\s*(\S+)", full_text)
    if m:
        spokesperson_last = m.group(1).strip()
        raw_fields["spokesperson_last"] = spokesperson_last

    # ── Users ─────────────────────────────────────────────────────────────────
    users: list[ESAFUser] = []
    seen: set[str] = set()

    # Training table: "On-site Erik Binter 02-SEP-26 OK ..."
    for tm in re.finditer(
        r"^(On-site|Remote)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z'\-\.]+)+)"
        r"\s+\d{2}-[A-Z]+-\d{2,4}",
        full_text, re.MULTILINE,
    ):
        name = tm.group(2).strip()
        if name not in seen:
            seen.add(name)
            users.append(ESAFUser(name=name, institution="", role=""))

    # Fallback: Authorization signature block
    if not users:
        for sm in re.finditer(
            r"^([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z'\-\.]+)+) +(.+?) +_{4,}",
            full_text, re.MULTILINE,
        ):
            name = sm.group(1).strip()
            inst = sm.group(2).strip()
            if name not in seen and len(name) < 60:
                seen.add(name)
                users.append(ESAFUser(name=name, institution=inst, role=""))

    # Enrich institutions from Authorization signature block.
    # Anchor each search to the known user name to avoid greedy-match errors
    # (institution words start with capitals, confusing a generic name pattern).
    for user in users:
        if user.institution:
            continue
        sm = re.search(
            re.escape(user.name) + r"[ ]+([^\n]+?)[ ]+_{4,}",
            full_text, re.MULTILINE,
        )
        if sm:
            inst = sm.group(1).strip()
            if len(inst) > 3 and not inst.isupper():
                user.institution = inst

    # Tag PI by spokesperson last name
    if spokesperson_last:
        last_lower = spokesperson_last.lower()
        for user in users:
            if user.name.split()[-1].lower() == last_lower:
                user.role = "PI"
                break
    for user in users:
        if not user.role:
            user.role = "user"

    confidence["users"] = 0.9 if users else 0.0

    for k, v in extracted.items():
        raw_fields.setdefault(k, v)
    raw_fields["full_text_preview"] = full_text[:2000]

    record = ESAFRecord(
        esaf_id=extracted.get("esaf_id", "UNKNOWN"),
        title=extracted.get("title", ""),
        start_date=extracted.get("start_date", ""),
        end_date=extracted.get("end_date", ""),
        beamline=extracted.get("beamline", ""),
        proposal_id=extracted.get("proposal_id", ""),
        users=users,
        source="pdf",
        raw_fields=raw_fields,
    )

    return record, confidence


# ── Convenience function ───────────────────────────────────────────────────────

def fetch_esaf(esaf_id: str, settings: dict) -> ESAFRecord | None:
    """Try server → local cache → return None.

    settings: connection settings dict with keys ``esaf_server_url`` and
    ``esaf_api_key`` (both optional globals, not per-profile).
    Successful server fetches are saved to the local cache.
    """
    server_url = (settings.get("esaf_server_url") or "").strip()
    api_key = (settings.get("esaf_api_key") or "").strip()

    if server_url:
        try:
            client = ESAFServerClient(server_url, api_key)
            record = client.get_esaf(esaf_id)
            if record:
                save_cached(record)
                return record
        except Exception:
            pass

    return load_cached(esaf_id)

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
    _path = Path.home() / ".easy_bluesky" / "pi_groups.json"

    @classmethod
    def load(cls) -> list[PIGroup]:
        """Return all PIGroups from the local registry file."""
        if not cls._path.exists():
            return []
        try:
            data = json.loads(cls._path.read_text(encoding="utf-8"))
            return [_group_from_dict(d) for d in data]
        except Exception:
            return []

    @classmethod
    def save(cls, groups: list[PIGroup]):
        """Persist the full list of PIGroups to the registry file."""
        cls._path.parent.mkdir(parents=True, exist_ok=True)
        cls._path.write_text(
            json.dumps([_group_to_dict(g) for g in groups], indent=2),
            encoding="utf-8",
        )

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
    """Parse various date string formats into YYYY-MM-DD. Returns '' on failure."""
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
    # DD-Mon-YYYY
    m = re.match(r"^(\d{1,2})-([A-Za-z]+)-(\d{4})$", raw)
    if m:
        mname = m.group(2).lower()
        if mname in _MONTHS:
            return f"{m.group(3)}-{_MONTHS[mname]:02d}-{int(m.group(1)):02d}"
    return ""


def _extract_first_date(text: str) -> str:
    """Find and parse the first date-like substring in text."""
    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text)
    if m:
        return _parse_date(m.group(0))
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return _parse_date(m.group(0))
    m = re.search(r"\d{1,2}-[A-Za-z]+-\d{4}", text)
    if m:
        return _parse_date(m.group(0))
    return ""


def parse_esaf_pdf(path_or_bytes) -> tuple[ESAFRecord, dict]:
    """Parse an ESAF PDF locally using pdfplumber.

    Returns (ESAFRecord, confidence_dict).
    Confidence values: 1.0 = clean regex match, 0.7 = some ambiguity, 0.0 = not found.

    Raises ImportError (with install hint) if pdfplumber is not installed.

    Patterns matched (case-insensitive):
    - ESAF ID/Number/#   → esaf_id
    - Experiment Title   → title
    - Start/End Date     → start_date / end_date
    - Beamline           → beamline
    - Proposal Number/ID → proposal_id
    - User table rows    → users list
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required for local PDF parsing.\n"
            "Install it with:  pip install pdfplumber"
        )

    import io

    if isinstance(path_or_bytes, (str, Path)):
        pdf = pdfplumber.open(str(path_or_bytes))
    else:
        pdf = pdfplumber.open(io.BytesIO(path_or_bytes))

    # Extract all text across pages
    pages_text = []
    with pdf as _pdf:
        for page in _pdf.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t)
    full_text = "\n".join(pages_text)

    def _find_field(patterns: list[tuple[str, float]]) -> tuple[str, float]:
        """Return (value, confidence) for the first matching pattern."""
        for pat, conf in patterns:
            m = re.search(pat, full_text, re.IGNORECASE | re.MULTILINE)
            if m:
                val = m.group(1).strip().rstrip(":").strip()
                if val:
                    return val, conf
        return "", 0.0

    # ESAF ID
    esaf_id, esaf_conf = _find_field([
        (r"ESAF\s*(?:ID|Number|No\.?|#)\s*[:\-]?\s*(\d+)", 1.0),
        (r"Experiment\s+Safety\s+Approval\s+Form\s*[:\-#]?\s*(\d+)", 0.9),
        (r"(?:^|\s)ESAF\s*[:\-]?\s*(\d{4,})", 0.8),
        (r"(?:Number|ID|#)\s*[:\-]?\s*(\d{5,})", 0.7),
    ])

    # Title
    title, title_conf = _find_field([
        (r"Experiment\s+Title\s*[:\-]?\s*(.+?)(?:\n|$)", 1.0),
        (r"^Title\s*[:\-]\s*(.+?)(?:\n|$)", 0.9),
        (r"Proposal\s+Title\s*[:\-]?\s*(.+?)(?:\n|$)", 0.7),
    ])

    # Start date
    start_raw, start_conf = _find_field([
        (r"(?:Experiment\s+)?Start\s+Date\s*[:\-]?\s*([\d/\-A-Za-z,\s]+?)(?:\n|End|$)", 1.0),
        (r"(?:^|\n)Start\s*[:\-]\s*([\d/\-A-Za-z,\s]+?)(?:\n|End|$)", 0.8),
        (r"From\s*[:\-]?\s*([\d/\-]+)", 0.7),
    ])
    start_date = ""
    if start_raw:
        start_date = _parse_date(start_raw.split("\n")[0].strip())
        if not start_date:
            start_date = _extract_first_date(start_raw)
            if start_date:
                start_conf = 0.7

    # End date
    end_raw, end_conf = _find_field([
        (r"(?:Experiment\s+)?End\s+Date\s*[:\-]?\s*([\d/\-A-Za-z,\s]+?)(?:\n|$)", 1.0),
        (r"(?:^|\n)End\s*[:\-]\s*([\d/\-A-Za-z,\s]+?)(?:\n|$)", 0.8),
        (r"To\s*[:\-]?\s*([\d/\-]+)", 0.7),
    ])
    end_date = ""
    if end_raw:
        end_date = _parse_date(end_raw.split("\n")[0].strip())
        if not end_date:
            end_date = _extract_first_date(end_raw)
            if end_date:
                end_conf = 0.7

    # Beamline
    beamline, beamline_conf = _find_field([
        (r"Beamline\s*[:\-]?\s*([A-Za-z0-9\-\s]+?)(?:\n|$)", 1.0),
        (r"Sector\s*[:\-]?\s*([A-Za-z0-9\-\s]+?)(?:\n|$)", 0.7),
    ])
    beamline = beamline.strip()

    # Proposal ID
    proposal_id, prop_conf = _find_field([
        (r"GUP\s*[:\-]?\s*(\d+)", 1.0),
        (r"Proposal\s+(?:Number|ID|#)\s*[:\-]?\s*(\w[\w\-]*)", 1.0),
        (r"Proposal\s*[:\-]?\s*(\d{5,})", 0.8),
    ])

    # Users — try to extract from user/experimenter table sections
    users: list[ESAFUser] = []

    user_section_m = re.search(
        r"(?:User|Experimenter|Participant)s?\s*(?:List|Table|:)?\s*\n(.*?)(?:\n{2,}|\Z)",
        full_text,
        re.IGNORECASE | re.DOTALL,
    )
    if user_section_m:
        user_section = user_section_m.group(1)
        for line in user_section.splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip header rows
            if re.match(r"(?i)^\s*(name|institution|role|email|experimenter|user)", line):
                continue
            # Split on 2+ spaces or tabs
            parts = re.split(r"[ \t]{2,}|\t", line)
            if len(parts) >= 2 and parts[0] and not re.match(r"^\d+$", parts[0]):
                u = ESAFUser(
                    name=parts[0].strip(),
                    institution=parts[1].strip() if len(parts) > 1 else "",
                    role=parts[2].strip() if len(parts) > 2 else "",
                    email=parts[3].strip() if len(parts) > 3 else "",
                )
                users.append(u)

    # Fallback: look for PI line
    if not users:
        pi_m = re.search(
            r"(?:Principal\s+Investigator|PI)\s*[:\-]?\s*(.+?)(?:\n|$)",
            full_text,
            re.IGNORECASE,
        )
        if pi_m:
            users.append(ESAFUser(name=pi_m.group(1).strip(), role="PI"))

    confidence = {
        "esaf_id":     esaf_conf,
        "title":       title_conf,
        "start_date":  start_conf if start_date else 0.0,
        "end_date":    end_conf if end_date else 0.0,
        "beamline":    beamline_conf if beamline else 0.0,
        "proposal_id": prop_conf,
        "users":       0.8 if users else 0.0,
    }

    record = ESAFRecord(
        esaf_id=esaf_id or "UNKNOWN",
        title=title,
        start_date=start_date,
        end_date=end_date,
        beamline=beamline,
        proposal_id=proposal_id,
        users=users,
        source="pdf",
        raw_fields={"full_text_preview": full_text[:2000]},
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

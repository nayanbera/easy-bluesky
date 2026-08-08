"""FastAPI application for the ESAF server."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import load_config
from .models import (
    ESAFCreate,
    ESAFRecord,
    ESAFUser,
    PIGroup,
    PIGroupCreate,
    ParsedPDFResult,
)
from .repository import ESAFRepository, PIGroupRepository, get_repositories

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_config = load_config()
_esaf_repo, _pigroup_repo = get_repositories(_config)

app = FastAPI(
    title="ESAF Server",
    description="Experiment Safety Assessment Form storage service for synchrotron beamlines.",
    version="1.0.0",
)

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_slug(univ_short_name: str, first_name: str, last_name: str) -> str:
    raw = f"{univ_short_name}_{first_name}_{last_name}"
    return raw.lower().replace(" ", "_").replace(",", "").replace("-", "_")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_api_key(x_api_key: Annotated[Optional[str], Header()] = None) -> None:
    """Dependency: enforce API key for write operations if one is configured."""
    required = _config.get("server", {}).get("api_key", "")
    if not required:
        return  # no key configured — allow everything
    if x_api_key != required:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header.",
        )


WriteAuth = Annotated[None, Depends(_require_api_key)]


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": _config.get("backend", "sqlite")}


# ============================================================
# REST API — ESAFs
# ============================================================

@app.get("/api/esafs", response_model=list[ESAFRecord])
def api_list_esafs(
    pi_group: Optional[str] = None,
    beamline: Optional[str] = None,
    search: Optional[str] = None,
) -> list[ESAFRecord]:
    return _esaf_repo.list(pi_group_slug=pi_group, beamline=beamline, search=search)


@app.get("/api/esafs/{esaf_id}", response_model=ESAFRecord)
def api_get_esaf(esaf_id: str) -> ESAFRecord:
    rec = _esaf_repo.get(esaf_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"ESAF {esaf_id!r} not found.")
    return rec


@app.post("/api/esafs", response_model=ESAFRecord, status_code=201)
def api_create_esaf(_auth: WriteAuth, body: ESAFCreate) -> ESAFRecord:
    now = _now()
    record = ESAFRecord(
        **body.model_dump(),
        pdf_available=False,
        created_at=now,
        updated_at=now,
    )
    return _esaf_repo.save(record)


@app.put("/api/esafs/{esaf_id}", response_model=ESAFRecord)
def api_update_esaf(_auth: WriteAuth, esaf_id: str, body: ESAFCreate) -> ESAFRecord:
    existing = _esaf_repo.get(esaf_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"ESAF {esaf_id!r} not found.")
    updated = existing.model_copy(
        update={**body.model_dump(exclude_unset=True), "esaf_id": esaf_id}
    )
    return _esaf_repo.save(updated)


@app.delete("/api/esafs/{esaf_id}", status_code=204)
def api_delete_esaf(_auth: WriteAuth, esaf_id: str) -> Response:
    if not _esaf_repo.delete(esaf_id):
        raise HTTPException(status_code=404, detail=f"ESAF {esaf_id!r} not found.")
    return Response(status_code=204)


@app.post("/api/esafs/parse-pdf", response_model=ParsedPDFResult)
def api_parse_pdf(
    _auth: WriteAuth,
    file: UploadFile = File(...),
) -> ParsedPDFResult:
    from .pdf_parser import parse_esaf_pdf

    data = file.file.read()
    try:
        result = parse_esaf_pdf(data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"PDF parse error: {exc}")
    return result


@app.post("/api/esafs/{esaf_id}/pdf", status_code=204)
def api_upload_pdf(
    _auth: WriteAuth,
    esaf_id: str,
    file: UploadFile = File(...),
) -> Response:
    data = file.file.read()
    if not _esaf_repo.save_pdf(esaf_id, data):
        raise HTTPException(status_code=500, detail="Failed to store PDF.")
    return Response(status_code=204)


@app.get("/api/esafs/{esaf_id}/pdf")
def api_download_pdf(esaf_id: str) -> Response:
    data = _esaf_repo.get_pdf(esaf_id)
    if data is None:
        raise HTTPException(status_code=404, detail="PDF not found.")
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{esaf_id}.pdf"'},
    )


# ============================================================
# REST API — PI Groups
# ============================================================

@app.get("/api/pi_groups", response_model=list[PIGroup])
def api_list_pi_groups() -> list[PIGroup]:
    return _pigroup_repo.list()


@app.get("/api/pi_groups/match/{member_name}", response_model=list[PIGroup])
def api_match_pi_groups(member_name: str) -> list[PIGroup]:
    return _pigroup_repo.find_by_member(member_name)


@app.get("/api/pi_groups/{slug}", response_model=PIGroup)
def api_get_pi_group(slug: str) -> PIGroup:
    group = _pigroup_repo.get(slug)
    if group is None:
        raise HTTPException(status_code=404, detail=f"PI group {slug!r} not found.")
    return group


@app.post("/api/pi_groups", response_model=PIGroup, status_code=201)
def api_create_pi_group(_auth: WriteAuth, body: PIGroupCreate) -> PIGroup:
    now = _now()
    group = PIGroup(**body.model_dump(), created_at=now)
    return _pigroup_repo.save(group)


@app.put("/api/pi_groups/{slug}", response_model=PIGroup)
def api_update_pi_group(_auth: WriteAuth, slug: str, body: PIGroupCreate) -> PIGroup:
    existing = _pigroup_repo.get(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"PI group {slug!r} not found.")
    updated = existing.model_copy(
        update={**body.model_dump(exclude_unset=True), "slug": slug}
    )
    return _pigroup_repo.save(updated)


@app.delete("/api/pi_groups/{slug}", status_code=204)
def api_delete_pi_group(_auth: WriteAuth, slug: str) -> Response:
    if not _pigroup_repo.delete(slug):
        raise HTTPException(status_code=404, detail=f"PI group {slug!r} not found.")
    return Response(status_code=204)


# ============================================================
# Admin HTML — ESAFs
# ============================================================

@app.get("/admin", response_class=HTMLResponse)
def admin_esaf_list(
    request: Request,
    pi_group: Optional[str] = None,
    beamline: Optional[str] = None,
    search: Optional[str] = None,
) -> HTMLResponse:
    esafs = _esaf_repo.list(pi_group_slug=pi_group, beamline=beamline, search=search)
    groups = _pigroup_repo.list()
    return templates.TemplateResponse(
        "esaf_list.html",
        {
            "request": request,
            "esafs": esafs,
            "groups": groups,
            "pi_group": pi_group or "",
            "beamline": beamline or "",
            "search": search or "",
        },
    )


@app.get("/admin/esafs/new", response_class=HTMLResponse)
def admin_esaf_new(request: Request) -> HTMLResponse:
    groups = _pigroup_repo.list()
    return templates.TemplateResponse(
        "esaf_form.html",
        {
            "request": request,
            "esaf": None,
            "groups": groups,
            "action": "/admin/esafs",
            "title": "New ESAF",
        },
    )


@app.get("/admin/esafs/{esaf_id}", response_class=HTMLResponse)
def admin_esaf_detail(request: Request, esaf_id: str) -> HTMLResponse:
    rec = _esaf_repo.get(esaf_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="ESAF not found.")
    return templates.TemplateResponse(
        "esaf_detail.html",
        {"request": request, "esaf": rec},
    )


@app.post("/admin/esafs", response_class=HTMLResponse)
async def admin_esaf_create(request: Request) -> RedirectResponse:
    form = await request.form()
    now = _now()
    users = _parse_users_from_form(form)
    record = ESAFRecord(
        esaf_id=form.get("esaf_id", "").strip(),
        title=form.get("title", "").strip(),
        start_date=form.get("start_date", "").strip(),
        end_date=form.get("end_date", "").strip(),
        beamline=form.get("beamline", "").strip(),
        proposal_id=form.get("proposal_id", "").strip(),
        pi_group_slug=form.get("pi_group_slug", "").strip(),
        source=form.get("source", "manual").strip(),
        users=users,
        pdf_available=False,
        created_at=now,
        updated_at=now,
    )
    _esaf_repo.save(record)
    return RedirectResponse(
        url=f"/admin/esafs/{record.esaf_id}", status_code=303
    )


@app.post("/admin/esafs/upload-pdf", response_class=HTMLResponse)
async def admin_upload_pdf(
    request: Request,
    file: UploadFile = File(...),
) -> HTMLResponse:
    from .pdf_parser import parse_esaf_pdf

    data = await file.read()
    try:
        result = parse_esaf_pdf(data)
    except Exception as exc:
        return templates.TemplateResponse(
            "esaf_form.html",
            {
                "request": request,
                "esaf": None,
                "groups": _pigroup_repo.list(),
                "action": "/admin/esafs",
                "title": "New ESAF (PDF parse error)",
                "error": str(exc),
            },
        )

    # Temporarily store parsed PDF under the extracted ID so we can upload later
    if result.record.esaf_id and result.record.esaf_id != "UNKNOWN":
        _esaf_repo.save_pdf(result.record.esaf_id, data)

    groups = _pigroup_repo.list()
    return templates.TemplateResponse(
        "esaf_form.html",
        {
            "request": request,
            "esaf": result.record,
            "groups": groups,
            "action": "/admin/esafs",
            "title": "Review Parsed ESAF",
            "confidence": result.confidence,
        },
    )


@app.post("/admin/esafs/{esaf_id}", response_class=HTMLResponse)
async def admin_esaf_update(request: Request, esaf_id: str) -> RedirectResponse:
    form = await request.form()
    existing = _esaf_repo.get(esaf_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="ESAF not found.")
    users = _parse_users_from_form(form)
    updated = existing.model_copy(
        update={
            "title": form.get("title", existing.title).strip(),
            "start_date": form.get("start_date", existing.start_date).strip(),
            "end_date": form.get("end_date", existing.end_date).strip(),
            "beamline": form.get("beamline", existing.beamline).strip(),
            "proposal_id": form.get("proposal_id", existing.proposal_id).strip(),
            "pi_group_slug": form.get("pi_group_slug", existing.pi_group_slug).strip(),
            "source": form.get("source", existing.source).strip(),
            "users": users if users else existing.users,
        }
    )
    _esaf_repo.save(updated)
    return RedirectResponse(url=f"/admin/esafs/{esaf_id}", status_code=303)


# ============================================================
# Admin HTML — PI Groups
# ============================================================

@app.get("/admin/pi_groups", response_class=HTMLResponse)
def admin_pi_group_list(request: Request) -> HTMLResponse:
    groups = _pigroup_repo.list()
    return templates.TemplateResponse(
        "pi_group_list.html",
        {"request": request, "groups": groups},
    )


@app.get("/admin/pi_groups/new", response_class=HTMLResponse)
def admin_pi_group_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "pi_group_form.html",
        {
            "request": request,
            "group": None,
            "action": "/admin/pi_groups",
            "title": "New PI Group",
        },
    )


@app.get("/admin/pi_groups/{slug}", response_class=HTMLResponse)
def admin_pi_group_detail(request: Request, slug: str) -> HTMLResponse:
    group = _pigroup_repo.get(slug)
    if group is None:
        raise HTTPException(status_code=404, detail="PI group not found.")
    return templates.TemplateResponse(
        "pi_group_form.html",
        {
            "request": request,
            "group": group,
            "action": f"/admin/pi_groups/{slug}",
            "title": f"Edit PI Group: {slug}",
        },
    )


@app.post("/admin/pi_groups", response_class=HTMLResponse)
async def admin_pi_group_create(request: Request) -> RedirectResponse:
    form = await request.form()
    now = _now()
    members_raw = form.get("known_members", "")
    members = [m.strip() for m in members_raw.splitlines() if m.strip()]

    univ = form.get("univ_short_name", "").strip()
    first = form.get("pi_first_name", "").strip()
    last = form.get("pi_last_name", "").strip()
    slug = form.get("slug", "").strip() or _make_slug(univ, first, last)

    group = PIGroup(
        slug=slug,
        pi_first_name=first,
        pi_last_name=last,
        pi_institution=form.get("pi_institution", "").strip(),
        univ_short_name=univ,
        known_members=members,
        created_at=now,
    )
    _pigroup_repo.save(group)
    return RedirectResponse(url=f"/admin/pi_groups/{slug}", status_code=303)


@app.post("/admin/pi_groups/{slug}", response_class=HTMLResponse)
async def admin_pi_group_update(request: Request, slug: str) -> RedirectResponse:
    form = await request.form()
    existing = _pigroup_repo.get(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="PI group not found.")
    members_raw = form.get("known_members", "")
    members = [m.strip() for m in members_raw.splitlines() if m.strip()]
    updated = existing.model_copy(
        update={
            "pi_first_name": form.get("pi_first_name", existing.pi_first_name).strip(),
            "pi_last_name": form.get("pi_last_name", existing.pi_last_name).strip(),
            "pi_institution": form.get("pi_institution", existing.pi_institution).strip(),
            "univ_short_name": form.get("univ_short_name", existing.univ_short_name).strip(),
            "known_members": members if members else existing.known_members,
        }
    )
    _pigroup_repo.save(updated)
    return RedirectResponse(url=f"/admin/pi_groups/{slug}", status_code=303)


# ============================================================
# Helpers
# ============================================================

def _parse_users_from_form(form) -> list[ESAFUser]:
    """Parse user rows from a multi-value HTML form."""
    users: list[ESAFUser] = []
    # Expect form fields: user_name_0, user_institution_0, user_role_0, user_email_0, ...
    i = 0
    while True:
        name = form.get(f"user_name_{i}", "").strip()
        if not name:
            break
        users.append(
            ESAFUser(
                name=name,
                institution=form.get(f"user_institution_{i}", "").strip(),
                role=form.get(f"user_role_{i}", "").strip(),
                email=form.get(f"user_email_{i}", "").strip(),
            )
        )
        i += 1
    return users


# ---------------------------------------------------------------------------
# Entry point for direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    server_cfg = _config.get("server", {})
    uvicorn.run(
        "esaf_server.main:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 8765),
        reload=False,
    )

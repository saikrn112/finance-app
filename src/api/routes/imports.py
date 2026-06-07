from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from src.ingestion.import_service import commit_import, load_preview, preview_import
from src.models import get_db

router = APIRouter()


@router.get("/sources")
def list_import_sources():
    from src.plugins.registry import get_source_options_for_frontend

    return get_source_options_for_frontend()


@router.post("/preview")
async def preview_upload(
    file: UploadFile = File(...),
    source: str = Form(...),
    kind: str | None = Form(None),
    db: Session = Depends(get_db),
):
    content = await file.read()
    try:
        return preview_import(db, file_bytes=content, filename=file.filename or "upload", source=source, kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{import_id}")
def get_import_preview(import_id: str):
    preview = load_preview(import_id)
    if not preview:
        raise HTTPException(status_code=404, detail="Import preview not found")
    return preview


@router.post("/{import_id}/commit")
def commit_upload(import_id: str, db: Session = Depends(get_db)):
    try:
        return commit_import(db, import_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Import preview not found")

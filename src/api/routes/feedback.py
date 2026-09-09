"""Feedback notes and their screenshots.

No `currency` parameter anywhere in this file, unlike every other money route: a note has no
amount. If that ever changes, it takes a required `currency` like the rest.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.data_paths import RUNTIME_ROOT
from src.models import AppMetadata, Feedback, FeedbackAttachment, get_db

router = APIRouter()

#: Where the last-issued note number is kept.
#:
#: A persistent counter, not `MAX(seq) + 1` over the surviving rows: deleting the newest note
#: would lower the maximum and the next note would be given a number that has already been used,
#: so "look at 12" would mean one note today and a different one tomorrow. Caught by a test that
#: was written before this was noticed.
SEQ_COUNTER_KEY = "feedback_last_seq"

#: Screenshots live on disk, not in the database.
#:
#: A pasted screenshot is easily hundreds of kilobytes. The database is backed up to the vault
#: in full, so embedding a handful of them would multiply the size of every backup for data
#: that compresses badly and is never queried.
ATTACHMENT_ROOT = RUNTIME_ROOT / "feedback_attachments"

#: Enough for a full-screen retina screenshot with room to spare, small enough that a stray
#: multi-megabyte file is rejected rather than silently stored.
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024

#: Magic-byte prefixes for the image formats a screenshot can plausibly be.
#:
#: Sniffed explicitly rather than with `imghdr`, which is deprecated and removed in Python 3.13 —
#: the bundle ships 3.12 today, so that would have been a working import that breaks on a routine
#: interpreter bump. Explicit prefixes also make the accepted set reviewable.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _image_kind(payload: bytes) -> str | None:
    """The image format of `payload`, or None if it is not one we accept."""
    for prefix, kind in _IMAGE_SIGNATURES:
        if payload.startswith(prefix):
            return kind
    # WebP is RIFF-framed: "RIFF" <4-byte size> "WEBP".
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    return None


def _attachment_path(attachment_id: str) -> Path:
    # Named from the row id, never from the uploaded filename: that is user-supplied, is not
    # unique, and could contain path separators.
    return ATTACHMENT_ROOT / f"{attachment_id}.bin"


class AttachmentItem(BaseModel):
    id: str
    filename: str
    byte_size: int
    created_at: datetime


class FeedbackItem(BaseModel):
    id: str
    seq: int
    body: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    attachments: list[AttachmentItem]


class FeedbackListResponse(BaseModel):
    items: list[FeedbackItem]
    open_count: int


class FeedbackCreate(BaseModel):
    body: str = Field(min_length=1)


class FeedbackUpdate(BaseModel):
    body: str | None = None
    resolved: bool | None = None


def _next_seq(db: Session) -> int:
    """The next note number, from a counter that never goes backwards.

    Seeded from the highest existing note so a database that predates the counter carries on
    from where it left off rather than restarting at 1 and colliding.
    """
    row = db.query(AppMetadata).filter(AppMetadata.key == SEQ_COUNTER_KEY).first()
    highest_existing = db.query(func.max(Feedback.seq)).scalar() or 0
    last = max(int(row.value) if row and row.value.isdigit() else 0, highest_existing)
    nxt = last + 1
    if row is None:
        db.add(AppMetadata(key=SEQ_COUNTER_KEY, value=str(nxt)))
    else:
        row.value = str(nxt)
    return nxt


def _serialize(note: Feedback, attachments: list[FeedbackAttachment]) -> FeedbackItem:
    return FeedbackItem(
        id=note.id,
        seq=note.seq,
        body=note.body,
        created_at=note.created_at,
        updated_at=note.updated_at,
        resolved_at=note.resolved_at,
        attachments=[
            AttachmentItem(
                id=a.id,
                filename=a.filename,
                byte_size=a.byte_size,
                created_at=a.created_at,
            )
            for a in attachments
        ],
    )


@router.get("/", response_model=FeedbackListResponse)
def list_feedback(db: Session = Depends(get_db)):
    notes = db.query(Feedback).order_by(Feedback.seq.desc()).all()
    # One query for every attachment rather than one per note: the list is read on every open
    # of the panel, and a note-at-a-time lookup makes that N+1 queries for no reason.
    grouped: dict[str, list[FeedbackAttachment]] = {}
    for attachment in db.query(FeedbackAttachment).order_by(FeedbackAttachment.created_at).all():
        grouped.setdefault(attachment.feedback_id, []).append(attachment)

    return FeedbackListResponse(
        items=[_serialize(note, grouped.get(note.id, [])) for note in notes],
        open_count=sum(1 for note in notes if note.resolved_at is None),
    )


@router.post("/", response_model=FeedbackItem)
def create_feedback(payload: FeedbackCreate, db: Session = Depends(get_db)):
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="A note needs some text")

    note = Feedback(seq=_next_seq(db), body=body)
    db.add(note)
    db.commit()
    db.refresh(note)
    return _serialize(note, [])


@router.patch("/{feedback_id}", response_model=FeedbackItem)
def update_feedback(feedback_id: str, payload: FeedbackUpdate, db: Session = Depends(get_db)):
    note = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")

    if payload.body is not None:
        body = payload.body.strip()
        # An empty edit means "no change", not "delete the note". Deleting is its own action, and
        # losing a note to a stray select-all-and-backspace would be unrecoverable.
        if body:
            note.body = body
    if payload.resolved is not None:
        note.resolved_at = datetime.utcnow() if payload.resolved else None
    note.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(note)

    attachments = (
        db.query(FeedbackAttachment)
        .filter(FeedbackAttachment.feedback_id == note.id)
        .order_by(FeedbackAttachment.created_at)
        .all()
    )
    return _serialize(note, attachments)


@router.delete("/{feedback_id}")
def delete_feedback(feedback_id: str, db: Session = Depends(get_db)):
    note = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")

    attachments = (
        db.query(FeedbackAttachment).filter(FeedbackAttachment.feedback_id == note.id).all()
    )
    # Files first, then rows. A leftover file is invisible clutter; a row pointing at a file that
    # is gone renders as a broken image every time the panel is opened.
    for attachment in attachments:
        _attachment_path(attachment.id).unlink(missing_ok=True)
        db.delete(attachment)
    db.delete(note)
    db.commit()
    return {"status": "deleted", "id": feedback_id}


@router.post("/{feedback_id}/attachments", response_model=AttachmentItem)
async def add_attachment(
    feedback_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    note = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="The file was empty")
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Images must be under {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB",
        )

    # Sniff the bytes rather than trusting the declared content type or the extension: this
    # endpoint writes a file to disk and serves it back, and the client decides both of those.
    if _image_kind(payload) is None:
        raise HTTPException(status_code=422, detail="Only images can be attached")

    attachment = FeedbackAttachment(
        feedback_id=note.id,
        filename=Path(file.filename or "image").name,
        byte_size=len(payload),
    )
    db.add(attachment)
    db.flush()  # assigns the id the filename is derived from

    ATTACHMENT_ROOT.mkdir(parents=True, exist_ok=True)
    path = _attachment_path(attachment.id)
    path.write_bytes(payload)
    # 0600: a screenshot of this app is a screenshot of the owner's finances.
    path.chmod(0o600)

    note.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(attachment)
    return AttachmentItem(
        id=attachment.id,
        filename=attachment.filename,
        byte_size=attachment.byte_size,
        created_at=attachment.created_at,
    )


@router.get("/attachments/{attachment_id}/image")
def get_attachment_image(attachment_id: str, db: Session = Depends(get_db)):
    attachment = (
        db.query(FeedbackAttachment).filter(FeedbackAttachment.id == attachment_id).first()
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail="No such attachment")
    path = _attachment_path(attachment.id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The image file is missing")
    # media_type is sniffed again on the way out rather than stored: the file on disk is the
    # authority, and a stored type could disagree with it after a restore.
    kind = _image_kind(path.read_bytes()[:16]) or "png"
    return FileResponse(path, media_type=f"image/{kind}", filename=attachment.filename)


@router.delete("/attachments/{attachment_id}")
def delete_attachment(attachment_id: str, db: Session = Depends(get_db)):
    attachment = (
        db.query(FeedbackAttachment).filter(FeedbackAttachment.id == attachment_id).first()
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail="No such attachment")
    _attachment_path(attachment.id).unlink(missing_ok=True)
    db.delete(attachment)
    db.commit()
    return {"status": "deleted", "id": attachment_id}

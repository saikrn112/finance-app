"""Notes written while using the app, and the screenshots attached to them.

Exists because the friction of "remember this, mention it later" loses most of it. A note is
jotted where it was noticed, and read later in one list.

Deliberately its own tables rather than a flag on something else: a note has no amount, no
date range and no currency, so it never takes part in a money query and does not need
excluding from all of them.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from src.models.database import Base
from src.models.transaction import generate_uuid


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(String, primary_key=True, default=generate_uuid)
    #: The number a note is *called*. Separate from `id` so it can be short enough to say out
    #: loud — "look at 12" — while the primary key stays a uuid like every other table here.
    seq = Column(Integer, nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    #: Set when the note has been dealt with, and kept rather than deleted, so the record of
    #: what was fixed survives. Deletion exists separately, for notes written by mistake.
    resolved_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_feedback_seq", "seq", unique=True),
        Index("ix_feedback_resolved", "resolved_at"),
    )


class FeedbackAttachment(Base):
    __tablename__ = "feedback_attachments"

    id = Column(String, primary_key=True, default=generate_uuid)
    feedback_id = Column(String, ForeignKey("feedback.id"), nullable=False)
    #: The name the file had when it was pasted or dropped. Shown to the user; never used to
    #: build a path, because it is not unique and can contain anything.
    filename = Column(String, nullable=False)
    byte_size = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_feedback_attachment_note", "feedback_id"),)

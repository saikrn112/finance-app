"""Tests for feedback notes and their attachments."""
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from src.api.server import app
from src.api.routes import feedback as feedback_route
from src.models import Feedback, FeedbackAttachment
from src.models.database import get_db


def _png(width: int = 2, height: int = 2) -> bytes:
    """A real, minimal PNG.

    Built rather than faked with a magic prefix, because the endpoint sniffs the bytes and a
    prefix-only stub would pass a check that a browser's upload would not.
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    """A client with an isolated database *and* an isolated attachment directory.

    The attachment root is module-level state derived from settings at import time, so it has to
    be redirected explicitly — otherwise these tests write into the real runtime directory.
    """
    Session, _ = temp_db
    monkeypatch.setattr(feedback_route, "ATTACHMENT_ROOT", tmp_path / "attachments")

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestCreateAndList:
    def test_a_new_note_appears_in_the_list(self, client):
        created = client.post("/api/feedback/", json={"body": "The ledger scrolls oddly"})
        assert created.status_code == 200
        assert created.json()["body"] == "The ledger scrolls oddly"

        listed = client.get("/api/feedback/")
        assert listed.status_code == 200
        payload = listed.json()
        assert [item["body"] for item in payload["items"]] == ["The ledger scrolls oddly"]
        assert payload["open_count"] == 1

    def test_notes_are_numbered_from_one_and_increment(self, client):
        first = client.post("/api/feedback/", json={"body": "first"}).json()
        second = client.post("/api/feedback/", json={"body": "second"}).json()
        assert (first["seq"], second["seq"]) == (1, 2)

    def test_a_deleted_note_does_not_free_its_number(self, client):
        """Two notes must never have been called the same thing.

        Numbering from a count would reuse the number of a deleted note, so "look at 2" would
        refer to one note today and a different one tomorrow.
        """
        first = client.post("/api/feedback/", json={"body": "first"}).json()
        second = client.post("/api/feedback/", json={"body": "second"}).json()
        client.delete(f"/api/feedback/{second['id']}")
        third = client.post("/api/feedback/", json={"body": "third"}).json()
        assert third["seq"] == 3
        assert third["seq"] != first["seq"]

    def test_newest_first(self, client):
        client.post("/api/feedback/", json={"body": "older"})
        client.post("/api/feedback/", json={"body": "newer"})
        bodies = [item["body"] for item in client.get("/api/feedback/").json()["items"]]
        assert bodies == ["newer", "older"]

    def test_whitespace_is_trimmed_and_blank_is_refused(self, client):
        assert client.post("/api/feedback/", json={"body": "  padded  "}).json()["body"] == "padded"
        assert client.post("/api/feedback/", json={"body": "   "}).status_code == 422
        assert client.post("/api/feedback/", json={"body": ""}).status_code == 422


class TestEdit:
    def test_the_text_can_be_changed(self, client):
        note = client.post("/api/feedback/", json={"body": "origonal"}).json()
        updated = client.patch(f"/api/feedback/{note['id']}", json={"body": "original"})
        assert updated.status_code == 200
        assert updated.json()["body"] == "original"

    def test_an_empty_edit_leaves_the_note_alone(self, client):
        """Deleting is a separate action; a stray select-all-and-backspace must not lose a note."""
        note = client.post("/api/feedback/", json={"body": "keep me"}).json()
        assert client.patch(f"/api/feedback/{note['id']}", json={"body": "   "}).json()["body"] == "keep me"

    def test_editing_bumps_updated_at_but_not_created_at(self, client):
        note = client.post("/api/feedback/", json={"body": "before"}).json()
        updated = client.patch(f"/api/feedback/{note['id']}", json={"body": "after"}).json()
        assert updated["created_at"] == note["created_at"]
        assert updated["updated_at"] >= note["updated_at"]

    def test_editing_a_missing_note_is_a_404(self, client):
        assert client.patch("/api/feedback/nope", json={"body": "x"}).status_code == 404


class TestResolve:
    def test_resolving_and_reopening(self, client):
        note = client.post("/api/feedback/", json={"body": "fix this"}).json()
        assert note["resolved_at"] is None

        resolved = client.patch(f"/api/feedback/{note['id']}", json={"resolved": True}).json()
        assert resolved["resolved_at"] is not None
        assert client.get("/api/feedback/").json()["open_count"] == 0

        reopened = client.patch(f"/api/feedback/{note['id']}", json={"resolved": False}).json()
        assert reopened["resolved_at"] is None
        assert client.get("/api/feedback/").json()["open_count"] == 1

    def test_a_resolved_note_is_kept_not_removed(self, client):
        """The record of what was fixed is the point; deletion is for mistakes."""
        note = client.post("/api/feedback/", json={"body": "done thing"}).json()
        client.patch(f"/api/feedback/{note['id']}", json={"resolved": True})
        ids = [item["id"] for item in client.get("/api/feedback/").json()["items"]]
        assert note["id"] in ids


class TestAttachments:
    def test_an_image_can_be_attached_and_fetched_back(self, client):
        note = client.post("/api/feedback/", json={"body": "see screenshot"}).json()
        image = _png()

        added = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("shot.png", image, "image/png")},
        )
        assert added.status_code == 200, added.text
        attachment = added.json()
        assert attachment["filename"] == "shot.png"
        assert attachment["byte_size"] == len(image)

        fetched = client.get(f"/api/feedback/attachments/{attachment['id']}/image")
        assert fetched.status_code == 200
        assert fetched.content == image
        assert fetched.headers["content-type"] == "image/png"

    def test_attachments_are_listed_with_their_note(self, client):
        note = client.post("/api/feedback/", json={"body": "with images"}).json()
        for name in ["one.png", "two.png"]:
            client.post(
                f"/api/feedback/{note['id']}/attachments",
                files={"file": (name, _png(), "image/png")},
            )
        item = client.get("/api/feedback/").json()["items"][0]
        assert [a["filename"] for a in item["attachments"]] == ["one.png", "two.png"]

    def test_a_non_image_is_refused(self, client):
        """The bytes are sniffed, not the declared type: the client controls both."""
        note = client.post("/api/feedback/", json={"body": "nope"}).json()
        response = client.post(
            f"/api/feedback/{note['id']}/attachments",
            # Claims to be a PNG and is not.
            files={"file": ("payload.png", b"#!/bin/sh\nrm -rf /\n", "image/png")},
        )
        assert response.status_code == 422

    def test_an_empty_file_is_refused(self, client):
        note = client.post("/api/feedback/", json={"body": "nope"}).json()
        response = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert response.status_code == 422

    def test_an_oversized_image_is_refused(self, client):
        note = client.post("/api/feedback/", json={"body": "huge"}).json()
        # A valid PNG header followed by enough padding to exceed the cap.
        oversized = _png() + b"\x00" * (feedback_route.MAX_ATTACHMENT_BYTES + 1)
        response = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("big.png", oversized, "image/png")},
        )
        assert response.status_code == 413

    def test_a_traversal_filename_cannot_escape_the_attachment_directory(self, client, tmp_path):
        """The filename is user-supplied and is never used to build a path."""
        note = client.post("/api/feedback/", json={"body": "sneaky"}).json()
        added = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("../../../../etc/passwd.png", _png(), "image/png")},
        )
        assert added.status_code == 200
        # Stored under the row id, inside the attachment root, with the name reduced to its
        # last component.
        assert added.json()["filename"] == "passwd.png"
        written = list((tmp_path / "attachments").iterdir())
        assert len(written) == 1
        assert written[0].name == f"{added.json()['id']}.bin"

    def test_the_stored_file_is_not_world_readable(self, client, tmp_path):
        note = client.post("/api/feedback/", json={"body": "private"}).json()
        added = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("s.png", _png(), "image/png")},
        ).json()
        path = tmp_path / "attachments" / f"{added['id']}.bin"
        # A screenshot of this app is a screenshot of the owner's finances.
        assert path.stat().st_mode & 0o077 == 0

    def test_an_attachment_can_be_removed_on_its_own(self, client, tmp_path):
        note = client.post("/api/feedback/", json={"body": "one image"}).json()
        added = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("s.png", _png(), "image/png")},
        ).json()
        path = tmp_path / "attachments" / f"{added['id']}.bin"
        assert path.exists()

        assert client.delete(f"/api/feedback/attachments/{added['id']}").status_code == 200
        assert not path.exists(), "the file should go with the row"
        assert client.get("/api/feedback/").json()["items"][0]["attachments"] == []

    def test_deleting_a_note_removes_its_images_from_disk(self, client, tmp_path):
        """Otherwise the attachment directory grows forever with unreachable files."""
        note = client.post("/api/feedback/", json={"body": "doomed"}).json()
        client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("s.png", _png(), "image/png")},
        )
        assert list((tmp_path / "attachments").iterdir())

        client.delete(f"/api/feedback/{note['id']}")
        assert list((tmp_path / "attachments").iterdir()) == []

    def test_attaching_to_a_missing_note_is_a_404(self, client):
        response = client.post(
            "/api/feedback/nope/attachments",
            files={"file": ("s.png", _png(), "image/png")},
        )
        assert response.status_code == 404

    def test_a_missing_file_on_disk_is_a_404_not_a_crash(self, client, tmp_path):
        """A restored database can reference images whose files were not restored."""
        note = client.post("/api/feedback/", json={"body": "orphan"}).json()
        added = client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("s.png", _png(), "image/png")},
        ).json()
        (tmp_path / "attachments" / f"{added['id']}.bin").unlink()
        assert client.get(f"/api/feedback/attachments/{added['id']}/image").status_code == 404


class TestDelete:
    def test_a_note_can_be_deleted(self, client):
        note = client.post("/api/feedback/", json={"body": "mistake"}).json()
        assert client.delete(f"/api/feedback/{note['id']}").status_code == 200
        assert client.get("/api/feedback/").json()["items"] == []

    def test_deleting_a_missing_note_is_a_404(self, client):
        assert client.delete("/api/feedback/nope").status_code == 404


class TestNoOrphanRows:
    def test_deleting_a_note_leaves_no_attachment_rows(self, client, temp_db):
        Session, _ = temp_db
        note = client.post("/api/feedback/", json={"body": "with image"}).json()
        client.post(
            f"/api/feedback/{note['id']}/attachments",
            files={"file": ("s.png", _png(), "image/png")},
        )
        client.delete(f"/api/feedback/{note['id']}")

        session = Session()
        try:
            assert session.query(Feedback).count() == 0
            assert session.query(FeedbackAttachment).count() == 0
        finally:
            session.close()

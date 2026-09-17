"""Snapshot backup: create, verify, publish, prune, promote, and archive statements.

Drive is faked at the `src.vault.google_drive` boundary rather than at
`src.vault.snapshot`'s own functions, so these exercise the real naming, ordering and pruning
logic. Stubbing one level higher would prove only that the module calls itself.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.vault import snapshot as snap


# --- helpers -------------------------------------------------------------------------------------


class FakeDrive:
    """Enough of Drive to exercise naming, listing, pruning and download."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.folders: dict[str, str] = {}
        self.trashed: list[str] = []
        self._next = 0

    def _id(self) -> str:
        self._next += 1
        return f"file-{self._next}"

    def ensure_visible_app_folder(self, token):
        return {"id": "app-root"}

    def ensure_child_folder(self, token, *, parent_id, name):
        key = f"{parent_id}/{name}"
        self.folders.setdefault(key, f"folder-{name}")
        return {"id": self.folders[key]}

    def list_drive_files(self, token, *, name=None, parent_id=None, mime_type=None):
        out = []
        for fid, row in self.files.items():
            if fid in self.trashed:
                continue
            if parent_id and row["parent"] != parent_id:
                continue
            if name and row["name"] != name:
                continue
            out.append({"id": fid, "name": row["name"], "size": str(len(row["content"]))})
        return out

    def upload_file_resumable(self, token, *, name, file_path, mime_type, parent_id=None, file_id=None):
        fid = file_id or self._id()
        self.files[fid] = {"name": name, "parent": parent_id, "content": Path(file_path).read_bytes()}
        return {"id": fid, "name": name}

    def upload_multipart_file(self, token, *, name, content_bytes, mime_type, parent_id=None, file_id=None):
        fid = file_id or self._id()
        self.files[fid] = {"name": name, "parent": parent_id, "content": content_bytes}
        return {"id": fid, "name": name}

    def download_file_to_path(self, token, file_id, dest, *, expected_size=None, on_progress=None):
        Path(dest).write_bytes(self.files[file_id]["content"])

    def trash(self, token, file_id):
        self.trashed.append(file_id)


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    for name in (
        "ensure_visible_app_folder",
        "ensure_child_folder",
        "list_drive_files",
        "upload_file_resumable",
        "upload_multipart_file",
        "download_file_to_path",
    ):
        monkeypatch.setattr(f"src.vault.google_drive.{name}", getattr(fake, name))
    monkeypatch.setattr("src.sync.transport._trash_file", fake.trash)
    return fake


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """A database at settings.database.path, with a transactions table to count."""
    from src.config import settings

    path = tmp_path / "finances.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (id TEXT PRIMARY KEY, amount NUMERIC)")
    conn.executemany("INSERT INTO transactions VALUES (?,?)", [(f"t{i}", i) for i in range(7)])
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings.database, "path", str(path))
    return path


def _seed_snapshot(drive, when: datetime, *, content: bytes = b"x"):
    parent = drive.ensure_child_folder(None, parent_id="app-root", name=snap.SNAPSHOTS_FOLDER)["id"]
    return drive.upload_multipart_file(
        None, name=snap.snapshot_name(snap.timestamp_slug(when)),
        content_bytes=content, mime_type="x", parent_id=parent,
    )


# --- naming --------------------------------------------------------------------------------------


class TestNaming:
    def test_the_name_carries_the_timestamp_because_there_is_no_manifest(self):
        when = datetime(2026, 9, 17, 5, 24, 57, tzinfo=timezone.utc)
        name = snap.snapshot_name(snap.timestamp_slug(when))
        assert name == "finances-2026-09-17T05-24-57Z.db"
        assert snap.created_at_from_name(name) == "2026-09-17T05:24:57Z"

    def test_a_stray_file_without_a_stamp_is_ignored_rather_than_mis_sorted(self):
        assert snap.created_at_from_name("notes.txt") is None

    def test_lexicographic_order_is_chronological_order(self):
        base = datetime(2026, 9, 17, tzinfo=timezone.utc)
        names = [snap.snapshot_name(snap.timestamp_slug(base + timedelta(hours=h))) for h in (0, 5, 20)]
        assert sorted(names) == names, "sorting by name must equal sorting by time"


# --- create / verify ------------------------------------------------------------------------------


class TestCreate:
    def test_a_snapshot_is_a_usable_standalone_database(self, live_db, tmp_path):
        path = snap.create_snapshot(tmp_path / "out")
        stats = snap.verify_snapshot(path)
        assert stats["integrity"] == "ok"
        assert stats["transactions"] == 7

    def test_it_does_not_write_to_the_live_database(self, live_db, tmp_path):
        before = live_db.stat().st_mtime_ns
        snap.create_snapshot(tmp_path / "out")
        assert live_db.stat().st_mtime_ns == before

    def test_a_missing_database_is_a_clear_error_not_an_empty_backup(self, tmp_path, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings.database, "path", str(tmp_path / "nope.db"))
        with pytest.raises(HTTPException) as caught:
            snap.create_snapshot(tmp_path)
        assert caught.value.status_code == 400

    def test_a_truncated_snapshot_is_rejected(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"not a database")
        with pytest.raises(HTTPException):
            snap.verify_snapshot(broken)


# --- publish / prune -----------------------------------------------------------------------------


class TestPublish:
    def test_publish_uploads_one_database_and_its_config(self, live_db, drive, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Path("config.yaml").write_text("plaid:\n  client_id: x\n")

        result = snap.publish_snapshot("tok")

        names = sorted(row["name"] for row in drive.files.values())
        assert len(names) == 2, f"expected a db and a config, got {names}"
        assert any(n.startswith(snap.DB_PREFIX) and n.endswith(".db") for n in names)
        assert any(n.startswith(snap.CONFIG_PREFIX) for n in names)
        assert result["transactions"] == 7

    def test_the_upload_is_the_database_only_not_the_whole_data_directory(self, live_db, drive, tmp_path, monkeypatch):
        """The bundle it replaced swept in replay data, import scratch and .db fossils."""
        monkeypatch.chdir(tmp_path)
        data = tmp_path / "data"
        (data / "runtime" / "replay").mkdir(parents=True)
        replay = b"REPLAY-STACK-BYTES" * 100
        (data / "runtime" / "replay" / "finances-replay.db").write_bytes(replay)
        (data / "derived" / "preview_workspace").mkdir(parents=True)
        scratch = b"IMPORT-SCRATCH-BYTES" * 100
        (data / "derived" / "preview_workspace" / "scratch.pdf").write_bytes(scratch)

        snap.publish_snapshot("tok")

        # Assert on *what* travelled, not on a size threshold: a small SQLite file is still ~12 KB.
        uploaded = {row["name"]: row["content"] for row in drive.files.values()}
        assert all(
            name.startswith((snap.DB_PREFIX, snap.CONFIG_PREFIX)) for name in uploaded
        ), f"unexpected files uploaded: {sorted(uploaded)}"
        blob = b"".join(uploaded.values())
        assert replay not in blob and scratch not in blob, (
            "replay data and import scratch must not reach Drive"
        )

    def test_retention_keeps_the_newest_and_trashes_the_rest(self, live_db, drive, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        for day in range(6):
            _seed_snapshot(drive, base + timedelta(days=day))

        removed = snap.prune_snapshots("tok", retention=3)

        kept = [row["name"] for row in snap.list_snapshots("tok")]
        assert len(kept) == 3
        assert kept == sorted(kept, reverse=True), "newest first"
        assert all(name < kept[-1] for name in removed), "only older ones are removed"

    def test_pruning_takes_the_config_sibling_with_it(self, live_db, drive, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        parent = drive.ensure_child_folder(None, parent_id="app-root", name=snap.SNAPSHOTS_FOLDER)["id"]
        old = datetime(2026, 8, 1, tzinfo=timezone.utc)
        _seed_snapshot(drive, old)
        drive.upload_multipart_file(
            None, name=snap.config_name(snap.timestamp_slug(old)),
            content_bytes=b"cfg", mime_type="x", parent_id=parent,
        )
        _seed_snapshot(drive, datetime(2026, 9, 1, tzinfo=timezone.utc))

        snap.prune_snapshots("tok", retention=1)

        left = {row["name"] for row in drive.list_drive_files(None, parent_id=parent)}
        assert not any(n.startswith(snap.CONFIG_PREFIX) for n in left), "orphaned config left behind"

    def test_retention_of_zero_is_treated_as_no_pruning_not_delete_everything(self, live_db, drive):
        _seed_snapshot(drive, datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert snap.prune_snapshots("tok", retention=0) == []
        assert len(snap.list_snapshots("tok")) == 1


# --- restore -------------------------------------------------------------------------------------


class TestRestore:
    def test_a_snapshot_round_trips_through_drive(self, live_db, drive, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        published = snap.publish_snapshot("tok")

        landed = snap.download_snapshot("tok", published["file_id"], tmp_path / "back.db")

        assert snap.verify_snapshot(landed)["transactions"] == 7

    def test_promotion_keeps_the_database_it_displaced(self, live_db, tmp_path):
        """Restoring the wrong snapshot is a mistake someone will make."""
        incoming = snap.create_snapshot(tmp_path / "incoming")

        result = snap.promote_snapshot(incoming)

        assert Path(settings_db_path()).exists()
        assert result["displaced"] and Path(result["displaced"]).exists()
        assert snap.verify_snapshot(Path(settings_db_path()))["transactions"] == 7

    def test_promotion_clears_sidecars_belonging_to_the_replaced_database(self, live_db, tmp_path):
        for suffix in ("-wal", "-shm"):
            live_db.with_name(live_db.name + suffix).write_bytes(b"stale")
        incoming = snap.create_snapshot(tmp_path / "incoming")

        snap.promote_snapshot(incoming)

        for suffix in ("-wal", "-shm"):
            assert not live_db.with_name(live_db.name + suffix).exists(), (
                "a stale sidecar lets SQLite replay old pages over the restored snapshot"
            )

    def test_an_unusable_snapshot_is_refused_before_the_live_database_is_touched(self, live_db, tmp_path):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"nonsense")
        before = live_db.read_bytes()

        with pytest.raises(HTTPException):
            snap.promote_snapshot(junk)

        assert live_db.read_bytes() == before, "the live database must survive a bad restore"


def settings_db_path() -> str:
    from src.config import settings

    return settings.database.path


# --- statements ----------------------------------------------------------------------------------


class TestStatements:
    def test_statements_upload_once_and_never_again(self, drive, tmp_path):
        raw = tmp_path / "raw"
        (raw / "amex").mkdir(parents=True)
        (raw / "amex" / "jan.pdf").write_bytes(b"statement one")
        (raw / "amex" / "feb.csv").write_bytes(b"statement two")

        first = snap.archive_statements("tok", source_dir=raw)
        second = snap.archive_statements("tok", source_dir=raw)

        assert first["uploaded"] == 2
        assert second == {"uploaded": 0, "skipped": 2, "bytes": 0}, (
            "re-sending unchanged statements is most of what made the old bundle enormous"
        )

    def test_names_are_content_addressed_so_identical_files_are_stored_once(self, drive, tmp_path):
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "a.pdf").write_bytes(b"same bytes")
        (raw / "copy-of-a.pdf").write_bytes(b"same bytes")

        result = snap.archive_statements("tok", source_dir=raw)

        assert result["uploaded"] == 1 and result["skipped"] == 1
        assert len(drive.files) == 1

    def test_the_extension_is_kept_so_the_files_stay_openable(self, drive, tmp_path):
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "jan.PDF").write_bytes(b"x")

        snap.archive_statements("tok", source_dir=raw)

        assert next(iter(drive.files.values()))["name"].endswith(".pdf")

    def test_a_missing_raw_directory_is_not_an_error(self, drive, tmp_path):
        assert snap.archive_statements("tok", source_dir=tmp_path / "absent")["uploaded"] == 0


class TestDriveListingPagination:
    """`list_drive_files` must return every match, not the first page.

    Found against real data: the statement archive re-uploaded 131 documents on its second run
    because the "already uploaded" set was silently truncated at 100 files, so every lookup past
    the first page answered "no". Any caller asking "does this already exist?" was affected.
    """

    def test_every_page_is_followed(self, monkeypatch):
        from src.vault import google_drive

        pages = [
            {"files": [{"id": f"a{i}", "name": f"a{i}"} for i in range(1000)],
             "nextPageToken": "page-2"},
            {"files": [{"id": f"b{i}", "name": f"b{i}"} for i in range(150)]},
        ]
        seen: list[str | None] = []

        def fake_request(request):
            from urllib.parse import parse_qs, urlparse

            token = parse_qs(urlparse(request.full_url).query).get("pageToken", [None])[0]
            seen.append(token)
            return pages[0] if token is None else pages[1]

        monkeypatch.setattr(google_drive, "_json_request", fake_request)

        files = google_drive.list_drive_files("tok", parent_id="folder")

        assert len(files) == 1150, "a truncated listing makes existence checks lie"
        assert seen == [None, "page-2"], "the next page token must be followed"

    def test_a_single_page_does_not_loop(self, monkeypatch):
        from src.vault import google_drive

        calls = []

        def fake_request(request):
            calls.append(1)
            return {"files": [{"id": "only", "name": "only"}]}

        monkeypatch.setattr(google_drive, "_json_request", fake_request)

        assert len(google_drive.list_drive_files("tok")) == 1
        assert len(calls) == 1

"""Rescuing the newest legacy `.fvault` bundles as snapshots before the old path is deleted.

The bundles are the only backups predating the snapshot design. What makes conversion worth doing
rather than just deleting them is the ratio: a 500 MB archive yields a ~10 MB database, because the
rest was the replay stack, import scratch and `.db` repair fossils the old sweep collected.
"""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.vault import legacy_convert as lc
from src.vault import snapshot as snap


def _database(path: Path, rows: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (id TEXT PRIMARY KEY, amount NUMERIC)")
    conn.executemany("INSERT INTO transactions VALUES (?,?)", [(f"t{i}", i) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def _stale_index_database(path: Path) -> Path:
    """A database whose table is intact but whose index is missing rows.

    Built by hiding the index from SQLite's schema, inserting while it cannot be maintained, then
    restoring the definition at its original rootpage -- which reproduces the real bundle's
    "wrong # of entries in index ix_category" / "row N missing from index" exactly.
    """
    _database(path, rows=50)
    conn = sqlite3.connect(path)
    conn.execute("CREATE INDEX ix_category ON transactions(amount)")
    conn.commit()
    rootpage, sql = conn.execute(
        "SELECT rootpage, sql FROM sqlite_master WHERE name = 'ix_category'"
    ).fetchone()
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("DELETE FROM sqlite_master WHERE name = 'ix_category'")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO transactions VALUES (?,?)", [(f"x{i}", i) for i in range(10)]
    )
    conn.commit()
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "INSERT INTO sqlite_master (type,name,tbl_name,rootpage,sql) VALUES ('index',?,?,?,?)",
        ("ix_category", "transactions", rootpage, sql),
    )
    conn.commit()
    conn.close()
    return path


def _bundle(
    tmp_path: Path,
    *,
    name: str = "bundle.fvault",
    created_at: str = "2026-06-05T01:22:43.752Z",
    rows: int = 5,
    with_config: bool = True,
    with_junk: bool = True,
) -> Path:
    """A bundle shaped the way `build_backup_bundle` wrote them."""
    staging = tmp_path / "staging"
    db = _database(staging / "state" / "app_data" / "runtime" / "prod" / "finances.db", rows)
    manifest = {
        "manifest_version": 2,
        "created_at": created_at,
        "vault_id": "00000000-0000-4000-8000-000000000001",
        "backup_id": "00000000-0000-4000-8000-0000000000aa",
    }
    (staging / "backup.manifest.json").write_text(json.dumps(manifest))
    if with_config:
        (staging / "state" / "config.yaml").write_text("plaid:\n  client_id: example\n")
    if with_junk:
        # What made these archives 500 MB: the replay stack, import scratch and repair fossils.
        # Incompressible, because PDFs and databases are: `b"JUNK" * n` would deflate to nothing
        # and the archive would come out smaller than the database it contains.
        for relative in (
            "state/app_data/runtime/replay/finances-replay.db",
            "state/app_data/runtime/prod/finances.pre_repair.db",
            "state/app_data/derived/preview_workspace/scratch.pdf",
        ):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(os.urandom(64 * 1024))

    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(staging).as_posix())
    return archive


# --- reading one member out of a bundle -----------------------------------------------------------


class TestExtract:
    def test_the_primary_database_comes_out_usable(self, tmp_path):
        result = lc.extract_from_bundle(_bundle(tmp_path), tmp_path / "out")

        assert result["integrity"] == "ok"
        assert result["transactions"] == 5
        assert result["created_at"] == "2026-06-05T01:22:43.752Z"

    def test_only_the_database_and_config_are_written_to_disk(self, tmp_path):
        """Extracting the whole archive would write ~700 MB to reach a 10 MB file."""
        out = tmp_path / "out"
        lc.extract_from_bundle(_bundle(tmp_path), out)

        written = sorted(p.name for p in out.rglob("*") if p.is_file())
        assert written == ["finances.db"]

    def test_the_config_travels_so_the_snapshot_pair_is_complete(self, tmp_path):
        result = lc.extract_from_bundle(_bundle(tmp_path), tmp_path / "out")
        assert b"client_id" in result["config_bytes"]

    def test_a_bundle_without_a_config_is_still_convertible(self, tmp_path):
        result = lc.extract_from_bundle(
            _bundle(tmp_path, with_config=False), tmp_path / "out"
        )
        assert result["config_bytes"] is None
        assert result["transactions"] == 5

    def test_the_replay_and_repair_databases_are_never_chosen(self, tmp_path):
        """`finances-replay.db` and `finances.pre_*.db` are both `.db` under app_data."""
        sizes = {
            "state/app_data/runtime/replay/finances-replay.db": 400_000_000,
            "state/app_data/runtime/prod/finances.pre_repair.db": 9_000_000,
            "state/app_data/runtime/prod/finances.db": 10_000_000,
        }
        assert lc.database_members(sizes)[0] == "state/app_data/runtime/prod/finances.db"

    def test_an_empty_decoy_database_does_not_win_on_alphabetical_order(self):
        """Found against the real vault: `finance.db` sorts before `finances.db`.

        Two months of history reported "no such table: transactions" because a 4 KB leftover was
        picked over the 10 MB database beside it. Candidates are ordered by size, and the caller
        tries the next one when a candidate does not verify.
        """
        sizes = {
            "state/app_data/runtime/prod/finance.db": 4096,
            "state/app_data/runtime/prod/finances.db": 10_000_000,
        }
        assert lc.database_members(sizes)[0] == "state/app_data/runtime/prod/finances.db"

    def test_a_decoy_is_skipped_in_favour_of_the_one_that_verifies(self, tmp_path):
        staging = tmp_path / "s"
        real = _database(staging / "state/app_data/runtime/prod/finances.db", rows=7)
        # An empty database with no `transactions` table, as older installs left behind.
        decoy = staging / "state/app_data/runtime/prod/aaa-decoy.db"
        sqlite3.connect(decoy).close()
        archive = tmp_path / "decoy.fvault"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(decoy, arcname="state/app_data/runtime/prod/aaa-decoy.db")
            zf.write(real, arcname="state/app_data/runtime/prod/finances.db")

        result = lc.extract_from_bundle(archive, tmp_path / "out")

        assert result["transactions"] == 7
        assert result["member"].endswith("finances.db")

    def test_a_stale_index_is_rebuilt_rather_than_the_month_discarded(self, tmp_path):
        """One real bundle failed with "row N missing from index ix_category".

        The rows are intact and only an index is stale, so REINDEX recovers it. Refusing the bundle
        would have discarded a month of history over something rebuildable.
        """
        staging = tmp_path / "s"
        db = _stale_index_database(staging / "state/app_data/runtime/prod/finances.db")
        with pytest.raises(HTTPException, match="integrity_check"):
            snap.verify_snapshot(db)  # the corruption is real, not assumed

        archive = tmp_path / "stale.fvault"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(db, arcname="state/app_data/runtime/prod/finances.db")

        result = lc.extract_from_bundle(archive, tmp_path / "out")

        assert result["repaired"] is True
        assert result["transactions"] == 60, "every row survives the rebuild"

    def test_repair_reports_failure_for_a_database_it_cannot_fix(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"not a database")
        assert lc.repair_indexes(broken) is False

    def test_a_bundle_with_no_database_is_a_clear_error(self, tmp_path):
        archive = tmp_path / "empty.fvault"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("backup.manifest.json", "{}")
        with pytest.raises(HTTPException) as caught:
            lc.extract_from_bundle(archive, tmp_path / "out")
        assert caught.value.status_code == 400

    def test_a_corrupt_database_inside_a_bundle_is_refused(self, tmp_path):
        staging = tmp_path / "bad"
        target = staging / "state" / "app_data" / "runtime" / "prod" / "finances.db"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not a database at all")
        archive = tmp_path / "bad.fvault"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(target, arcname="state/app_data/runtime/prod/finances.db")

        with pytest.raises(HTTPException):
            lc.extract_from_bundle(archive, tmp_path / "out")


class TestNaming:
    def test_the_snapshot_keeps_the_bundles_own_timestamp(self):
        """Otherwise a months-old backup sorts as if it were taken today."""
        slug = lc._slug_from_created_at("2026-06-05T01:22:43.752Z")
        assert snap.snapshot_name(slug) == "finances-2026-06-05T01-22-43Z.db"

    def test_a_bundle_with_no_usable_timestamp_falls_back_to_now(self):
        slug = lc._slug_from_created_at(None)
        assert snap.created_at_from_name(snap.snapshot_name(slug)) is not None


# --- the Drive round trip -------------------------------------------------------------------------


class FakeDrive:
    def __init__(self, archives):
        self.files: dict[str, dict] = {}
        self.uploaded: list[str] = []
        self._n = 0
        for path, created in archives:
            self.add(path.name, path.read_bytes(), created=created, parent="backups")

    def _id(self):
        self._n += 1
        return f"id-{self._n}"

    def add(self, name, content, *, created="", parent="root"):
        fid = self._id()
        self.files[fid] = {"name": name, "content": content, "created": created, "parent": parent,
                           "mime": "application/octet-stream"}
        return fid

    def ensure_visible_app_folder(self, token):
        return {"id": "root"}

    def ensure_child_folder(self, token, *, parent_id, name):
        return {"id": f"{parent_id}/{name}"}

    def list_drive_files(self, token, *, name=None, parent_id=None, mime_type=None):
        out = []
        if parent_id == "root":
            out.append({"id": "backups", "name": "backups",
                        "mimeType": "application/vnd.google-apps.folder"})
        for fid, row in self.files.items():
            if row["parent"] != parent_id:
                continue
            out.append({"id": fid, "name": row["name"], "mimeType": row["mime"],
                        "size": str(len(row["content"])), "createdTime": row["created"]})
        return out

    def download_file_to_path(self, token, file_id, dest, *, expected_size=None, on_progress=None):
        Path(dest).write_bytes(self.files[file_id]["content"])

    def upload_file_resumable(self, token, *, name, file_path, mime_type, parent_id=None, file_id=None):
        self.uploaded.append(name)
        return {"id": self.add(name, Path(file_path).read_bytes(), parent=parent_id)}

    def upload_multipart_file(self, token, *, name, content_bytes, mime_type, parent_id=None, file_id=None):
        self.uploaded.append(name)
        return {"id": self.add(name, content_bytes, parent=parent_id)}


@pytest.fixture
def drive_with(monkeypatch):
    def build(archives):
        fake = FakeDrive(archives)
        for name in (
            "ensure_visible_app_folder", "ensure_child_folder", "list_drive_files",
            "download_file_to_path", "upload_file_resumable", "upload_multipart_file",
        ):
            monkeypatch.setattr(f"src.vault.google_drive.{name}", getattr(fake, name))
        return fake

    return build


class TestConvert:
    def test_the_newest_bundles_become_snapshots(self, tmp_path, drive_with):
        old = _bundle(tmp_path / "a", created_at="2026-05-07T00:47:30.000Z", rows=3)
        new = _bundle(tmp_path / "b", created_at="2026-06-05T01:22:43.000Z", rows=9)
        drive = drive_with([(old, "2026-05-07T00:47:30Z"), (new, "2026-06-05T01:22:43Z")])

        result = lc.convert_newest(None, count=1)

        assert [row["name"] for row in result["converted"]] == [
            "finances-2026-06-05T01-22-43Z.db"
        ], "the newest bundle, by creation time, is the one converted"
        assert result["converted"][0]["transactions"] == 9
        assert "config-2026-06-05T01-22-43Z.yaml" in drive.uploaded

    def test_the_snapshot_is_a_fraction_of_the_bundle(self, tmp_path, drive_with):
        archive = _bundle(tmp_path / "a")
        drive_with([(archive, "2026-06-05T01:22:43Z")])

        row = lc.convert_newest(None, count=1)["converted"][0]

        assert row["size_bytes"] < row["from_bytes"], (
            "the bundle's bulk was replay data and scratch, not the database"
        )

    def test_rerunning_skips_what_is_already_converted(self, tmp_path, drive_with):
        archive = _bundle(tmp_path / "a")
        drive_with([(archive, "2026-06-05T01:22:43Z")])

        lc.convert_newest(None, count=1)
        again = lc.convert_newest(None, count=1)

        assert again["converted"] == []
        assert again["skipped"] == ["finances-2026-06-05T01-22-43Z.db"]

    def test_one_unreadable_bundle_does_not_stop_the_others(self, tmp_path, drive_with):
        good = _bundle(tmp_path / "a", created_at="2026-06-05T01:22:43.000Z")
        broken = tmp_path / "broken.fvault"
        broken.write_bytes(b"not a zip file")
        drive_with([(good, "2026-06-05T01:22:43Z"), (broken, "2026-06-06T00:00:00Z")])

        result = lc.convert_newest(None, count=2)

        assert len(result["converted"]) == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["name"] == "broken.fvault"

    def test_nothing_to_convert_is_not_an_error(self, drive_with):
        drive_with([])
        assert lc.convert_newest(None, count=3)["converted"] == []

    def test_archives_are_found_however_deep_they_sit(self, tmp_path, drive_with):
        """`<vault_id>/backups/<backup_id>/x.fvault` is three levels below the root."""
        archive = _bundle(tmp_path / "a")
        drive_with([(archive, "2026-06-05T01:22:43Z")])

        found = lc.find_legacy_archives(None)

        assert [row["name"] for row in found] == ["bundle.fvault"]


class TestOrdering:
    """Found against the real vault: a server-side copy stamps today onto every file it copies.

    Sorting by Drive's `createdTime` therefore put copies of April backups at the top and would have
    spent all three conversion slots on the oldest history in the vault.
    """

    def test_the_backup_time_comes_from_the_filename_not_from_drive(self, tmp_path, drive_with):
        april = _bundle(tmp_path / "a", name="vault-x-2026-04-17T08-28-51Z.fvault")
        june = _bundle(tmp_path / "b", name="vault-x-2026-06-05T01-22-43Z.fvault")
        # The April bundle was copied later, so Drive reports it as the newer file.
        drive_with([(april, "2026-09-20T04:09:37Z"), (june, "2026-06-05T01:22:43Z")])

        found = lc.find_legacy_archives(None)

        assert [row["backup_at"] for row in found] == [
            "2026-06-05T01-22-43Z", "2026-04-17T08-28-51Z"
        ]

    def test_a_copy_of_the_same_backup_does_not_consume_a_slot(self, tmp_path, drive_with):
        original = _bundle(tmp_path / "a", name="vault-x-2026-06-05T01-22-43Z.fvault")
        older = _bundle(tmp_path / "b", name="vault-x-2026-05-07T00-46-33Z.fvault")
        drive_with([
            (original, "2026-06-05T01:22:43Z"),
            (original, "2026-09-20T04:09:37Z"),  # the copy: same name, same bytes
            (older, "2026-05-07T00:46:33Z"),
        ])

        found = lc.find_legacy_archives(None)

        assert len(found) == 2, "the copy and its original are one backup"
        assert found[0]["created_time"] == "2026-06-05T01:22:43Z", "the original, not the copy"

    def test_monthly_selection_covers_history_instead_of_three_adjacent_days(self):
        """The live vault's newest three bundles were all inside days `snapshots/` already held."""
        archives = [
            {"backup_at": "2026-09-17T22-36-38Z"},
            {"backup_at": "2026-09-17T05-24-57Z"},
            {"backup_at": "2026-09-15T23-11-11Z"},
            {"backup_at": "2026-08-02T00-00-00Z"},
            {"backup_at": "2026-06-05T01-22-43Z"},
        ]

        assert [a["backup_at"][:7] for a in lc.select_archives(archives, count=3)] == [
            "2026-09", "2026-09", "2026-09"
        ], "count-based selection preserves one week of a five-month history"

        assert [a["backup_at"] for a in lc.select_archives(archives, monthly=True)] == [
            "2026-09-17T22-36-38Z", "2026-08-02T00-00-00Z", "2026-06-05T01-22-43Z"
        ], "one per month, and the newest within each"

    def test_an_archive_with_no_stamp_in_its_name_still_shows_up_last(self, tmp_path, drive_with):
        stamped = _bundle(tmp_path / "a", name="vault-x-2026-06-05T01-22-43Z.fvault")
        odd = _bundle(tmp_path / "b", name="mystery.fvault")
        drive_with([(stamped, "2026-06-05T01:22:43Z"), (odd, "2026-07-01T00:00:00Z")])

        found = lc.find_legacy_archives(None)

        assert [row["name"] for row in found] == [
            "vault-x-2026-06-05T01-22-43Z.fvault", "mystery.fvault"
        ], "unstamped archives sort after the ones whose real time is known"

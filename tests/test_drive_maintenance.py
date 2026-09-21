"""Copying the vault, and removing only what is provably redundant.

The duplicates exist because Drive lets several files share a name in one folder, and the
content-addressed archive relied on a name lookup that silently truncated at 100 results before
pagination was fixed: 493 files for 231 documents on the real account.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.vault import drive_maintenance as dm
from src.vault import snapshot as snap

FOLDER = dm.FOLDER_MIME


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.trashed: set[str] = set()
        self._n = 0

    def _id(self):
        self._n += 1
        return f"id-{self._n}"

    def ensure_visible_app_folder(self, token):
        return self.mkdir(None, "Finance Vault")

    def ensure_child_folder(self, token, *, parent_id, name):
        for fid, row in self.files.items():
            if row["parent"] == parent_id and row["name"] == name and row["mime"] == FOLDER:
                return {"id": fid}
        return self.mkdir(parent_id, name)

    def mkdir(self, parent, name):
        for fid, row in self.files.items():
            if row["parent"] == parent and row["name"] == name and row["mime"] == FOLDER:
                return {"id": fid}
        fid = self._id()
        self.files[fid] = {"name": name, "parent": parent, "mime": FOLDER, "size": 0, "created": ""}
        return {"id": fid}

    def add(self, parent, name, *, size=10, created="2026-01-01"):
        fid = self._id()
        self.files[fid] = {"name": name, "parent": parent, "mime": "application/pdf",
                           "size": size, "created": created}
        return fid

    def list_drive_files(self, token, *, name=None, parent_id=None, mime_type=None):
        out = []
        for fid, row in self.files.items():
            if fid in self.trashed or row["parent"] != parent_id:
                continue
            if name and row["name"] != name:
                continue
            out.append({"id": fid, "name": row["name"], "mimeType": row["mime"],
                        "size": str(row["size"]), "createdTime": row["created"]})
        return out

    def copy_file(self, token, file_id, *, name, parent_id):
        row = self.files[file_id]
        fid = self._id()
        self.files[fid] = {**row, "name": name, "parent": parent_id}
        return {"id": fid}

    def trash(self, token, file_id):
        self.trashed.add(file_id)


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    for name in ("ensure_visible_app_folder", "ensure_child_folder", "list_drive_files"):
        monkeypatch.setattr(f"src.vault.google_drive.{name}", getattr(fake, name))
    monkeypatch.setattr(dm, "copy_file", fake.copy_file)
    monkeypatch.setattr("src.sync.transport._trash_file", fake.trash)
    return fake


#: Obviously synthetic, and UUID-shaped, which is how a legacy vault folder is named.
LEGACY_VAULT_ID = "00000000-0000-4000-8000-000000000001"

ARCHIVE_SIZE = 500 * 1024 * 1024


def _vault(drive):
    root = drive.ensure_visible_app_folder(None)["id"]
    stmts = drive.ensure_child_folder(None, parent_id=root, name="statements")["id"]
    snaps = drive.ensure_child_folder(None, parent_id=root, name="snapshots")["id"]
    drive.ensure_child_folder(None, parent_id=root, name="devices")
    # Same document stored three times, as the pagination bug produced.
    drive.add(stmts, "aaa.pdf", size=1000, created="2026-01-01")
    drive.add(stmts, "aaa.pdf", size=1000, created="2026-01-02")
    drive.add(stmts, "aaa.pdf", size=1000, created="2026-01-03")
    drive.add(stmts, "bbb.pdf", size=500, created="2026-01-01")
    drive.add(snaps, "finances-2026-09-19T23-01-38Z.db", size=9000)
    # Legacy per-device vault. The archives sit three levels below the root --
    # <vault_id>/backups/<backup_id>/x.fvault -- which is the depth a shallow survey misses.
    legacy = drive.ensure_child_folder(None, parent_id=root, name=LEGACY_VAULT_ID)["id"]
    backups = drive.ensure_child_folder(None, parent_id=legacy, name="backups")["id"]
    for n in range(2):
        held = drive.ensure_child_folder(None, parent_id=backups, name=f"backup-{n}")["id"]
        drive.add(held, "old.fvault", size=ARCHIVE_SIZE)
    drive.add(root, "finance-app-vault-x-2026-04-17.fvault", size=300)
    return root


class TestCopy:
    def test_the_whole_tree_is_duplicated_server_side(self, drive):
        root = _vault(drive)
        result = dm.copy_folder(None, root, name="Finance Vault copy")
        # 4 statements + 1 snapshot + 2 legacy archives + 1 loose archive
        assert result["files_copied"] == 8
        assert result["failed"] == 0

    def test_a_single_unreadable_file_does_not_abandon_the_copy(self, drive, monkeypatch):
        root = _vault(drive)
        calls = {"n": 0}

        def flaky(token, file_id, *, name, parent_id):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("drive said no")
            return drive.copy_file(token, file_id, name=name, parent_id=parent_id)

        monkeypatch.setattr(dm, "copy_file", flaky)
        result = dm.copy_folder(None, root, name="copy")
        assert result["failed"] == 1
        assert result["files_copied"] == 7


class TestTidy:
    def test_preview_counts_duplicates_and_legacy_without_touching_anything(self, drive):
        _vault(drive)
        preview = dm.preview_tidy(None)
        assert preview["duplicate_statement_files"] == 2, "three copies of one name leaves two"
        assert [f["name"] for f in preview["legacy_vault_folders"]] == [LEGACY_VAULT_ID[:8] + "..."]
        assert preview["legacy_loose_files"] == ["finance-app-vault-x-2026-04-17.fvault"]
        assert drive.trashed == set(), "preview must not trash"

    def test_archives_three_levels_down_are_counted_not_reported_as_empty(self, drive):
        """A one-or-two level survey reported 28.5 GB of bundles as 0.0 MB.

        The intermediate `backups/` and `<backup_id>/` folders have no size of their own, so a walk
        that stops early sums zeroes and the preview claims there is nothing to reclaim.
        """
        _vault(drive)
        preview = dm.preview_tidy(None)
        assert preview["legacy_vault_folders"][0]["files"] == 2
        assert preview["legacy_gb"] == pytest.approx(
            (2 * ARCHIVE_SIZE + 300) / 1073741824, abs=0.01
        )

    def test_a_folder_that_is_not_a_vault_id_is_left_alone(self, drive):
        """The safety copy taken before tidying is a top-level folder that is not ours.

        Classifying "anything unrecognised" as legacy would trash the backup taken to protect the
        tidy, which is the one file you cannot afford to lose while tidying.
        """
        root = _vault(drive)
        copy = drive.ensure_child_folder(None, parent_id=root, name="Finance Vault copy 2026")["id"]
        drive.add(copy, "held.fvault", size=ARCHIVE_SIZE)

        preview = dm.preview_tidy(None)
        assert preview["left_alone"] == ["Finance Vault copy 2026"]

        dm.tidy(None)
        assert copy not in drive.trashed
        assert [f["name"] for f in drive.list_drive_files(None, parent_id=copy)] == ["held.fvault"]

    def test_a_vault_shaped_folder_holding_other_files_is_left_alone(self, drive):
        """A UUID name alone is not evidence; the archives are."""
        root = _vault(drive)
        other = "00000000-0000-4000-8000-0000000000ff"
        occupied = drive.ensure_child_folder(None, parent_id=root, name=other)["id"]
        drive.add(occupied, "notes.txt", size=5)

        dm.tidy(None)

        assert occupied not in drive.trashed
        assert dm.preview_tidy(None)["left_alone"] == [other]

    def test_an_emptied_legacy_husk_is_finally_removed(self, drive):
        """Drive sometimes refuses to trash a folder whose contents have already gone.

        The real vault left two such husks behind. Requiring a `.fvault` as the only evidence would
        have stranded them in the root permanently, reported as unrecognised on every run.
        """
        root = _vault(drive)
        husk = drive.ensure_child_folder(
            None, parent_id=root, name="00000000-0000-4000-8000-0000000000ee"
        )["id"]
        drive.ensure_child_folder(None, parent_id=husk, name="backups")

        dm.tidy(None)

        assert husk in drive.trashed
        assert dm.preview_tidy(None)["left_alone"] == []

    def test_a_legacy_manifest_named_with_a_hyphen_is_also_removed(self, drive):
        """Both `-latest.manifest.json` and `-manifest.json` were written; one was being missed."""
        root = _vault(drive)
        drive.add(root, "finance-app-vault-x-manifest.json", size=400)
        drive.add(root, "finance-app-vault-x-latest.manifest.json", size=400)

        dm.tidy(None)

        left = [f["name"] for f in drive.list_drive_files(None, parent_id=root)]
        assert not any("manifest.json" in name for name in left)

    def test_the_statements_manifest_is_not_mistaken_for_a_duplicate(self, drive):
        root = _vault(drive)
        stmts = drive.ensure_child_folder(None, parent_id=root, name="statements")["id"]
        drive.add(stmts, "manifest.json", size=20, created="2026-02-01")

        dm.tidy(None)

        names = [f["name"] for f in drive.list_drive_files(None, parent_id=stmts)]
        assert "manifest.json" in names, "the manifest is what makes the archive restorable"

    def test_one_copy_of_each_statement_survives(self, drive):
        root = _vault(drive)
        dm.tidy(None)
        stmts = drive.ensure_child_folder(None, parent_id=root, name="statements")["id"]
        names = sorted(f["name"] for f in drive.list_drive_files(None, parent_id=stmts))
        assert names == ["aaa.pdf", "bbb.pdf"]

    def test_the_oldest_copy_is_the_one_kept(self, drive):
        root = _vault(drive)
        dm.tidy(None)
        stmts = drive.ensure_child_folder(None, parent_id=root, name="statements")["id"]
        kept = [f for f in drive.list_drive_files(None, parent_id=stmts) if f["name"] == "aaa.pdf"]
        assert kept[0]["createdTime"] == "2026-01-01", "the earliest backup referred to this one"

    def test_snapshots_and_devices_are_never_touched(self, drive):
        root = _vault(drive)
        dm.tidy(None)
        snaps = drive.ensure_child_folder(None, parent_id=root, name="snapshots")["id"]
        assert len(drive.list_drive_files(None, parent_id=snaps)) == 1

    def test_keep_legacy_leaves_the_old_vaults_alone(self, drive):
        root = _vault(drive)
        result = dm.tidy(None, drop_legacy=False)
        assert result["legacy_files_trashed"] == 0
        names = [f["name"] for f in drive.list_drive_files(None, parent_id=root)]
        assert LEGACY_VAULT_ID in names

    def test_the_legacy_vault_is_trashed_bottom_up(self, drive):
        """Trashing the parent alone fails under the `drive.file` scope.

        The real vault answered `appNotAuthorizedToChild`: Drive will not trash a folder that still
        holds children the app cannot prove it may touch, so children go first, deepest first.
        """
        root = _vault(drive)
        result = dm.tidy(None)

        assert result["legacy_files_trashed"] == 3, "2 archives inside + 1 loose at the root"
        assert result["legacy_folders_trashed"] == 4, "2 backup folders, `backups`, and the vault"
        assert result["reclaimed_gb"] == pytest.approx(
            (2 * ARCHIVE_SIZE + 300) / 1073741824, abs=0.01
        )
        names = [f["name"] for f in drive.list_drive_files(None, parent_id=root)]
        assert LEGACY_VAULT_ID not in names

    def test_children_are_trashed_before_their_parents(self, drive, monkeypatch):
        order: list[str] = []

        def recording(token, file_id):
            order.append(file_id)
            drive.trash(token, file_id)

        monkeypatch.setattr("src.sync.transport._trash_file", recording)
        root = _vault(drive)
        vault_id = next(
            f["id"] for f in drive.list_drive_files(None, parent_id=root)
            if f["name"] == LEGACY_VAULT_ID
        )

        dm.tidy(None)

        assert order.index(vault_id) == len(order) - 2, (
            "the vault folder goes after everything inside it, before only the loose archive"
        )

    def test_a_folder_that_refuses_to_go_does_not_abandon_the_rest(self, drive, monkeypatch):
        """28 GB must not stay because one directory would not delete."""
        root = _vault(drive)
        stubborn = {"n": 0}

        def flaky(token, file_id):
            stubborn["n"] += 1
            if stubborn["n"] == 1:
                raise RuntimeError("drive said no")
            drive.trashed.add(file_id)

        monkeypatch.setattr("src.sync.transport._trash_file", flaky)

        result = dm.tidy(None)

        assert result["legacy_files_trashed"] >= 1
        names = [f["name"] for f in drive.list_drive_files(None, parent_id=root)]
        assert LEGACY_VAULT_ID not in names, "the vault folder still went"

    def test_it_is_idempotent(self, drive):
        _vault(drive)
        dm.tidy(None)
        assert dm.preview_tidy(None)["total_gb"] == 0

    def test_tidy_can_be_scoped_to_a_subfolder(self, drive):
        """So the legacy half of a safety copy can be dropped without touching its good half."""
        root = _vault(drive)
        copy = drive.ensure_child_folder(None, parent_id=root, name="Finance Vault copy")["id"]
        inner = drive.ensure_child_folder(None, parent_id=copy, name=LEGACY_VAULT_ID)["id"]
        drive.add(inner, "held.fvault", size=ARCHIVE_SIZE)
        keep = drive.ensure_child_folder(None, parent_id=copy, name="snapshots")["id"]
        drive.add(keep, "finances-2026-09-19T23-01-38Z.db", size=9000)

        result = dm.tidy(None, root_id=copy)

        assert result["legacy_files_trashed"] == 1
        assert inner in drive.trashed
        assert len(drive.list_drive_files(None, parent_id=keep)) == 1, "its snapshots must survive"


class TestStatementsManifest:
    def test_one_hash_records_every_path_it_stood_for(self, tmp_path):
        raw = tmp_path / "raw"
        (raw / "amex").mkdir(parents=True)
        (raw / "amex" / "jan.pdf").write_bytes(b"same")
        (raw / "amex" / "jan-copy.pdf").write_bytes(b"same")
        (raw / "amex" / "feb.pdf").write_bytes(b"different")

        manifest = snap.build_statements_manifest(raw)

        assert len(manifest["files"]) == 2, "two unique documents"
        paths = sorted(next(v for v in manifest["files"].values() if len(v) == 2))
        assert paths == ["amex/jan-copy.pdf", "amex/jan.pdf"], (
            "recording one path would restore one file where two existed"
        )

    def test_restore_rebuilds_every_original_path(self, tmp_path, drive, monkeypatch):
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "a.pdf").write_bytes(b"same")
        (raw / "b.pdf").write_bytes(b"same")
        manifest = snap.build_statements_manifest(raw)

        root = drive.ensure_visible_app_folder(None)["id"]
        stmts = drive.ensure_child_folder(None, parent_id=root, name="statements")["id"]
        name = next(iter(manifest["files"]))
        drive.add(stmts, name)
        monkeypatch.setattr(
            "src.vault.google_drive.download_file_to_path",
            lambda token, file_id, dest, **kw: Path(dest).write_bytes(b"same"),
        )

        out = tmp_path / "restored"
        result = snap.restore_statements(None, out, manifest=manifest)

        assert result == {"written": 2, "missing": 0}
        assert (out / "a.pdf").read_bytes() == b"same"
        assert (out / "b.pdf").read_bytes() == b"same"

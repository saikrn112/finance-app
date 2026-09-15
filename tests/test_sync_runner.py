"""Transport selection and the enable flag.

Two things matter here. Sync is **off unless asked** -- every existing install is in that state, and
sync publishes real financial history. And there is exactly **one transport**: Google Drive. A
directory-based one existed briefly and was removed, because a folder only reaches processes that can
see that filesystem, which made single-machine sync look like multi-device sync.
"""
import pytest

from src.sync import runner


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(runner.ENABLE_VAR, raising=False)
    # Guards against the removed setting coming back by the side door.
    monkeypatch.delenv("FINANCE_APP_SYNC_FOLDER", raising=False)


class TestEnableFlag:
    def test_off_when_unset(self):
        assert runner.sync_enabled() is False

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "   "])
    def test_off_for_falsey_values(self, monkeypatch, value):
        monkeypatch.setenv(runner.ENABLE_VAR, value)
        assert runner.sync_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_on_for_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv(runner.ENABLE_VAR, value)
        assert runner.sync_enabled() is True


class TestTransportChoice:
    def test_no_transport_when_disabled(self, db_session):
        choice = runner.choose_transport(db_session)
        assert choice.transport is None
        assert choice.mode == runner.MODE_OFF
        assert runner.ENABLE_VAR in choice.reason

    def test_drive_is_the_only_transport(self, db_session, monkeypatch):
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        monkeypatch.setattr(
            "src.api.routes.settings._google_access",
            lambda db: (None, "test-token", {}),
        )
        choice = runner.choose_transport(db_session)
        assert choice.mode == runner.MODE_DRIVE

    def test_a_sync_folder_variable_is_ignored(self, db_session, monkeypatch):
        """The removed setting must not quietly work if something still sets it."""
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        monkeypatch.setenv("FINANCE_APP_SYNC_FOLDER", "/tmp/should-be-ignored")
        monkeypatch.setattr(
            "src.api.routes.settings._google_access",
            lambda db: (None, "test-token", {}),
        )
        choice = runner.choose_transport(db_session)
        assert choice.mode == runner.MODE_DRIVE
        assert "should-be-ignored" not in choice.reason

    def test_no_folder_transport_exists_at_all(self):
        """A stronger form: the class is gone, so it cannot be reintroduced by accident."""
        import src.sync.transport as transport

        assert not hasattr(transport, "FolderTransport")

    def test_an_unconnected_drive_is_reported_not_raised(self, db_session, monkeypatch):
        """A background scheduler calls this. "Google Drive is not connected" is a thing for the user
        to do, not an exception to propagate out of a worker thread."""
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        choice = runner.choose_transport(db_session)
        assert choice.transport is None
        assert "no transport" in choice.reason


class TestSyncOnce:
    def test_does_nothing_and_does_not_raise_when_disabled(self, db_session):
        result = runner.sync_once(db_session)
        assert result["ran"] is False
        assert result["mode"] == runner.MODE_OFF

    def test_does_nothing_when_drive_is_not_connected(self, db_session, monkeypatch):
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        result = runner.sync_once(db_session)
        assert result["ran"] is False

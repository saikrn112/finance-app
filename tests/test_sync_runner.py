"""Transport selection and the enable flag.

The behaviour that matters most here is that sync is **off unless asked**: every existing install is
in that state after this ships, and sync writes real financial history to cloud storage.
"""
import pytest

from src.sync import runner


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(runner.ENABLE_VAR, raising=False)
    monkeypatch.delenv(runner.FOLDER_VAR, raising=False)


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

    def test_a_configured_folder_wins(self, db_session, monkeypatch, tmp_path):
        """No credentials needed, and the right answer for a shared volume."""
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        monkeypatch.setenv(runner.FOLDER_VAR, str(tmp_path))
        choice = runner.choose_transport(db_session)
        assert choice.mode == runner.MODE_FOLDER
        assert choice.transport is not None

    def test_drive_is_used_when_no_folder_is_set(self, db_session, monkeypatch):
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        monkeypatch.setattr(
            "src.api.routes.settings._google_access",
            lambda db: (None, "test-token", {}),
        )
        choice = runner.choose_transport(db_session)
        assert choice.mode == runner.MODE_DRIVE

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
        assert result == {"ran": False, "mode": runner.MODE_OFF, "reason": result["reason"]}

    def test_runs_against_a_folder(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setenv(runner.ENABLE_VAR, "1")
        monkeypatch.setenv(runner.FOLDER_VAR, str(tmp_path / "shared"))
        result = runner.sync_once(db_session, device_label="Test")
        assert result["ran"] is True
        assert result["published"] is True

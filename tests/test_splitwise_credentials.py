"""Where Splitwise credentials come from, and that secrets stay write-only.

Credentials can be typed into the app (stored in the database, which is gitignored) or left
in config.yaml. The app-entered values win, and no endpoint may ever echo a secret back.
"""
from src.integrations.splitwise import Credentials
from src.services import splitwise_credentials as store


def test_nothing_configured_is_not_usable(db_session):
    described = store.describe(db_session)
    assert described["usable"] is False
    assert described["auth_mode"] is None


def test_saved_oauth_credentials_become_usable(db_session):
    store.save(db_session, client_id="abc123456789", client_secret="shh")
    creds = store.resolve(db_session)
    assert creds.has_oauth is True
    described = store.describe(db_session)
    assert described["auth_mode"] == "oauth"
    assert described["client_id_source"] == "app"


def test_api_key_alone_is_usable_and_preferred(db_session):
    store.save(db_session, api_key="key-xyz")
    creds = store.resolve(db_session)
    assert creds.has_api_key is True
    # An API key needs no OAuth dance, so it wins when both are present.
    store.save(db_session, client_id="abc", client_secret="shh")
    assert store.describe(db_session)["auth_mode"] == "api_key"


def test_describe_never_returns_secrets(db_session):
    store.save(db_session, client_id="abc123456789", client_secret="super-secret", api_key="key-xyz")
    blob = repr(store.describe(db_session))
    assert "super-secret" not in blob
    assert "key-xyz" not in blob
    # A short hint is fine for recognising which app is configured.
    assert store.describe(db_session)["client_id_hint"] == "456789"


def test_partial_save_does_not_clear_other_fields(db_session):
    store.save(db_session, client_id="abc", client_secret="shh")
    store.save(db_session, client_id="def")  # secret omitted entirely
    creds = store.resolve(db_session)
    assert creds.client_id == "def"
    assert creds.client_secret == "shh", "omitting a field must not wipe it"


def test_blank_string_clears_a_field(db_session):
    store.save(db_session, api_key="key-xyz")
    store.save(db_session, api_key="")
    assert store.resolve(db_session).has_api_key is False


def test_clear_removes_everything_app_entered(db_session):
    store.save(db_session, client_id="abc", client_secret="shh")
    store.clear(db_session)
    assert store.describe(db_session)["usable"] is False


def test_redirect_uri_defaults_so_the_user_need_not_work_it_out(db_session):
    assert store.resolve(db_session).redirect_uri.endswith("/api/splitwise/callback")


def test_api_key_short_circuits_token_refresh():
    """No expiry handling for an API key — it is returned as-is."""
    from src.integrations.splitwise import ensure_fresh_access_token
    token, extra = ensure_fresh_access_token({}, Credentials(api_key="key-xyz"))
    assert token == "key-xyz"
    assert extra == {}

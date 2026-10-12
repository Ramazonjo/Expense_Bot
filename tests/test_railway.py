import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from expense_bot.config import RuntimeConfigurationError
from expense_bot.credentials import SCOPES, google_credentials


def test_railway_secret_is_loaded_without_creating_or_reading_a_file(settings, tmp_path):
    info = {
        "type": "service_account",
        "private_key": "line1\nline2\n",
        "client_email": "test@example.com",
    }
    settings.google_service_account_json = SecretStr(json.dumps(info))
    settings.google_service_account_file = tmp_path / "must-not-exist.json"
    with (
        patch("expense_bot.credentials.Credentials.from_service_account_info") as from_info,
        patch("expense_bot.credentials.Credentials.from_service_account_file") as from_file,
    ):
        assert google_credentials(settings) is from_info.return_value
        from_info.assert_called_once_with(info, scopes=SCOPES)
        from_file.assert_not_called()
    assert not settings.google_service_account_file.exists()
    assert "line1" not in repr(settings)


@pytest.mark.parametrize("value", ["", "not-json-SECRET", "[]", '{"type":"authorized_user"}'])
def test_invalid_secret_never_silently_uses_a_local_key_or_exposes_it(settings, value):
    settings.google_service_account_json = SecretStr(value)
    with patch("expense_bot.credentials.Credentials.from_service_account_file") as fallback:
        with pytest.raises(RuntimeConfigurationError) as exc:
            google_credentials(settings)
        assert "not-json-SECRET" not in str(exc.value)
        fallback.assert_not_called()


def test_local_google_file_still_supported(settings):
    with patch("expense_bot.credentials.Credentials.from_service_account_file") as loader:
        google_credentials(settings)
        loader.assert_called_once_with(str(settings.google_service_account_file), scopes=SCOPES)


def test_railway_refuses_unattached_volume(settings):
    settings.railway_project_id = "project-test"
    settings.railway_volume_mount_path = None
    with pytest.raises(RuntimeConfigurationError, match="attach a persistent Volume"):
        settings.validate_runtime_storage()


def test_railway_db_must_stay_inside_attached_volume(settings, tmp_path):
    mount = tmp_path / "volume"
    mount.mkdir()
    settings.railway_project_id = "project-test"
    settings.railway_volume_mount_path = mount
    settings.sqlite_path = mount / "bot.sqlite3"
    settings.validate_runtime_storage()
    settings.sqlite_path = tmp_path / "outside.sqlite3"
    with pytest.raises(RuntimeConfigurationError, match="SQLITE_PATH"):
        settings.validate_runtime_storage()
    settings.sqlite_path = Path("data/bot.sqlite3")
    with pytest.raises(RuntimeConfigurationError, match="SQLITE_PATH"):
        settings.validate_runtime_storage()


def test_local_mode_does_not_require_railway_volume(settings):
    settings.railway_project_id = None
    settings.validate_runtime_storage()


def test_bootstrap_mode_needs_no_google_or_openai_configuration(monkeypatch):
    from unittest.mock import AsyncMock

    from expense_bot import __main__ as entry

    monkeypatch.setattr("sys.argv", ["expense_bot"])
    monkeypatch.setenv("BOT_MODE", "id-only")
    monkeypatch.setattr(entry, "load_dotenv", lambda: None)
    with (
        patch.object(entry, "id_only", new_callable=AsyncMock) as bootstrap,
        patch.object(entry, "Settings", side_effect=AssertionError("No app settings needed")),
    ):
        entry.main()
        bootstrap.assert_awaited_once()

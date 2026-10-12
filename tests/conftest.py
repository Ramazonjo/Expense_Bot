import pytest

from expense_bot.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        telegram_bot_token="123456789:local_test_token",
        openai_api_key="local-test-no-api-access",
        google_spreadsheet_id="local_test_sheet",
        allowed_user_ids="100,200",
        report_chat_ids="100,200",
        sqlite_path=tmp_path / "test.sqlite3",
    )

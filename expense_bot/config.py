from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeConfigurationError(ValueError):
    """A deployment error with a safe, user-readable message (no secret values)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    telegram_bot_token: SecretStr
    openai_api_key: SecretStr
    openai_model: str = "gpt-4o"
    google_service_account_file: Path = Path("secrets/service-account.json")
    google_service_account_json: SecretStr | None = None
    google_spreadsheet_id: str
    google_worksheet_name: str = "Расходы"
    allowed_user_ids: str
    report_chat_ids: str
    timezone: str = "Asia/Dushanbe"
    currency: str = "TJS"
    report_hour: int = Field(default=23, ge=0, le=23)
    report_minute: int = Field(default=0, ge=0, le=59)
    report_previous_day: bool = False
    sqlite_path: Path = Path("data/bot.sqlite3")
    # Supplied by Railway automatically. Do not fill these manually.
    railway_project_id: str | None = None
    railway_volume_mount_path: Path | None = None
    max_image_mb: int = Field(default=10, ge=1, le=15)
    max_expenses: int = Field(default=50, ge=1, le=100)
    max_job_attempts: int = Field(default=5, ge=1, le=10)
    max_pending_jobs: int = Field(default=100, ge=1, le=1000)
    openai_timeout_seconds: int = Field(default=90, ge=10, le=300)
    google_timeout_seconds: int = Field(default=30, ge=5, le=120)

    @field_validator("allowed_user_ids", "report_chat_ids")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        parts = value.split(",")
        if not parts or any(not p.strip().isdigit() or int(p.strip()) <= 0 for p in parts):
            raise ValueError("Use positive numeric Telegram user IDs separated by commas")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.upper().strip()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Use a three-letter currency code, e.g. TJS")
        return value

    @field_validator("google_spreadsheet_id")
    @classmethod
    def validate_sheet_id(cls, value: str) -> str:
        if not value or not all(c.isalnum() or c in "_-" for c in value):
            raise ValueError("Use the spreadsheet ID, not its URL")
        return value

    @model_validator(mode="after")
    def recipients_are_authorized(self):
        if not set(self.report_ids).issubset(self.user_ids):
            raise ValueError("Every REPORT_CHAT_IDS entry must be in ALLOWED_USER_IDS")
        return self

    def validate_runtime_storage(self):
        """Refuse ephemeral SQLite on Railway to preserve retries across redeploys."""
        if not self.railway_project_id:
            return
        volume = self.railway_volume_mount_path
        if volume is None or not volume.is_absolute() or not volume.is_dir():
            raise RuntimeConfigurationError(
                "Railway: attach a persistent Volume at /data before starting this bot. "
                "RAILWAY_VOLUME_MOUNT_PATH must be supplied by Railway."
            )
        if not self.sqlite_path.is_absolute() or not self.sqlite_path.resolve().is_relative_to(
            volume.resolve()
        ):
            raise RuntimeConfigurationError(
                "Railway: SQLITE_PATH must point inside the attached Volume; "
                "use /data/bot.sqlite3 when the mount path is /data."
            )

    @property
    def user_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.allowed_user_ids.split(",")}

    @property
    def report_ids(self) -> list[int]:
        return sorted({int(x.strip()) for x in self.report_chat_ids.split(",")})

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.google_spreadsheet_id}/edit"

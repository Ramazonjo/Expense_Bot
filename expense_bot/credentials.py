import json

from google.oauth2.service_account import Credentials

from .config import RuntimeConfigurationError, Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def google_credentials(settings: Settings):
    """Prefer an in-memory Railway secret; retain local JSON-file support."""
    secret = settings.google_service_account_json
    if secret is not None:
        try:
            info = json.loads(secret.get_secret_value())
            if not isinstance(info, dict) or info.get("type") != "service_account":
                raise ValueError("Expected a Google service-account object")
            # JSON parsing restores the escaped newlines of private_key. Do not
            # globally replace '\\n': doing so can corrupt otherwise valid JSON.
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeConfigurationError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is invalid. Paste the complete original "
                "Google JSON into the Railway variable, without surrounding quotes."
            ) from exc
    return Credentials.from_service_account_file(
        str(settings.google_service_account_file), scopes=SCOPES
    )

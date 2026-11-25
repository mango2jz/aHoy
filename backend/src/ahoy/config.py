"""Settings loaded from the environment and backend/.env. Secrets are SecretStr."""

from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["gemini", "mock"]
GithubName = Literal["real", "mock"]

_BACKEND_DIR = Path(__file__).resolve().parents[2]
# Later files win: backend/.env overrides the repo-root .env. Missing files are fine.
_ENV_FILES = (_BACKEND_DIR.parent / ".env", _BACKEND_DIR / ".env")


class ConfigError(ValueError):
    """Raised when settings are missing or unsafe."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore")

    # Secrets
    gemini_api_key: SecretStr | None = None
    github_app_private_key: SecretStr | None = None
    github_client_secret: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    token_encryption_key: SecretStr | None = None
    database_url: SecretStr | None = None
    migration_database_url: SecretStr | None = None
    ahoy_owner_password: SecretStr | None = None
    ahoy_app_password: SecretStr | None = None

    # Non-secret
    app_origin: str = "http://127.0.0.1:5173"
    # Fail-safe default: unset means prod (https origin required, mock GitHub refused).
    ahoy_env: Literal["dev", "test", "prod"] = "prod"
    github_app_id: str | None = None
    github_app_slug: str | None = None
    github_client_id: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    allowed_models: str | None = None
    trusted_proxy_ips: str | None = None
    repo_cache_dir: str = ".repo-cache"
    repo_cache_max_gb: float = 20.0
    git_history_depth: int = 20
    max_concurrent_steps: int = 8
    user_max_running_steps: int = 2
    user_max_daily_steps: int = 200
    user_max_sessions: int = 50
    ahoy_sandbox_runtime: str | None = None
    runner_socket: str | None = None

    @model_validator(mode="after")
    def _check_origin(self) -> Self:
        if self.ahoy_env == "prod" and not self.app_origin.startswith("https://"):
            raise ConfigError("APP_ORIGIN must be https:// when AHOY_ENV=prod")
        return self

    def validate_for(self, provider: ProviderName, github: GithubName) -> None:
        """Fail with the missing variable's name; mock+mock needs no secrets."""
        required: dict[str, SecretStr | None] = {}
        if provider == "gemini":
            required["GEMINI_API_KEY"] = self.gemini_api_key
        if github == "real":
            required["GITHUB_APP_PRIVATE_KEY"] = self.github_app_private_key
            required["GITHUB_CLIENT_SECRET"] = self.github_client_secret
            required["GITHUB_WEBHOOK_SECRET"] = self.github_webhook_secret
            required["TOKEN_ENCRYPTION_KEY"] = self.token_encryption_key
        for name, value in required.items():
            if value is None or not value.get_secret_value():
                raise ConfigError(f"Missing required setting {name}")
        if github == "mock" and self.ahoy_env not in ("dev", "test"):
            raise ConfigError("--github mock is only allowed when AHOY_ENV is dev or test")

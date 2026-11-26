"""Settings loaded from the environment and backend/.env. Secrets are SecretStr."""

import os
import stat
import sys
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["gemini", "mock"]
GithubName = Literal["real", "mock"]

_BACKEND_DIR = Path(__file__).resolve().parents[2]
# Later files win: backend/.env overrides the repo-root .env. Missing files are fine.
_ENV_FILES = (_BACKEND_DIR.parent / ".env", _BACKEND_DIR / ".env")


def env_files() -> tuple[Path, ...] | None:
    """The .env files to load; None (no file at all) when AHOY_NO_ENV_FILE=1 (the E2E run)."""
    return None if os.environ.get("AHOY_NO_ENV_FILE") == "1" else _ENV_FILES


class ConfigError(ValueError):
    """Raised when settings are missing or unsafe."""


class Settings(BaseSettings):
    # env_ignore_empty: a copied .env.example has `NAME=` lines; treat them as unset.
    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore", env_ignore_empty=True)

    def __init__(self, **values: Any) -> None:
        # An explicit _env_file (tests) wins; otherwise AHOY_NO_ENV_FILE=1 turns .env off.
        values.setdefault("_env_file", env_files())
        super().__init__(**values)

    # Secrets
    gemini_api_key: SecretStr | None = None
    github_app_private_key: SecretStr | None = None
    # Path to the PEM (alternative to GITHUB_APP_PRIVATE_KEY). The path is not secret.
    github_app_private_key_file: Path | None = None
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
    gemini_model: str = "gemini-3.5-flash"  # 2.5-flash is closed to new keys (probe 2026-11-25)
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

    _key_from_file: SecretStr | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check_origin(self) -> Self:
        if self.ahoy_env == "prod" and not self.app_origin.startswith("https://"):
            raise ConfigError("APP_ORIGIN must be https:// when AHOY_ENV=prod")
        # The CSRF check compares Origin to this exactly: scheme://host[:port], nothing more.
        parts = urlsplit(self.app_origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or "@" in parts.netloc
            or f"{parts.scheme}://{parts.netloc}" != self.app_origin
        ):
            raise ConfigError(
                "APP_ORIGIN must be scheme://host[:port] with no path, trailing slash or query"
            )
        if self.ahoy_env == "prod" and "*" in (self.trusted_proxy_ips or ""):
            raise ConfigError("TRUSTED_PROXY_IPS must not contain * when AHOY_ENV=prod")
        return self

    def github_app_private_key_value(self) -> SecretStr | None:
        """The App key from GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_FILE (read once)."""
        path = self.github_app_private_key_file
        if path is None:
            return self.github_app_private_key
        if (
            self.github_app_private_key is not None
            and self.github_app_private_key.get_secret_value()
        ):
            raise ConfigError(
                "Set only one of GITHUB_APP_PRIVATE_KEY and GITHUB_APP_PRIVATE_KEY_FILE"
            )
        if self._key_from_file is not None:
            return self._key_from_file
        try:
            mode = path.stat().st_mode
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"GITHUB_APP_PRIVATE_KEY_FILE {path} is unreadable ({type(exc).__name__})"
            ) from None
        # Group/world-readable is fine (GitHub downloads keys as 0644); writable is not.
        if sys.platform != "win32" and mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ConfigError(
                f"GITHUB_APP_PRIVATE_KEY_FILE {path} must not be group- or world-writable"
            )
        if not text.startswith("-----BEGIN"):
            raise ConfigError(f"GITHUB_APP_PRIVATE_KEY_FILE {path} does not look like a PEM key")
        self._key_from_file = SecretStr(text)
        return self._key_from_file

    def validate_for(self, provider: ProviderName, github: GithubName) -> None:
        """Fail with the missing variable's name; mock+mock needs no secrets."""
        required: dict[str, SecretStr | None] = {}
        if provider == "gemini":
            required["GEMINI_API_KEY"] = self.gemini_api_key
        if github == "real":
            required["GITHUB_APP_PRIVATE_KEY"] = self.github_app_private_key_value()
            required["GITHUB_CLIENT_SECRET"] = self.github_client_secret
            required["GITHUB_WEBHOOK_SECRET"] = self.github_webhook_secret
            required["TOKEN_ENCRYPTION_KEY"] = self.token_encryption_key
        for name, value in required.items():
            if value is None or not value.get_secret_value():
                raise ConfigError(f"Missing required setting {name}")
        if github == "real":
            for name, plain in (
                ("GITHUB_CLIENT_ID", self.github_client_id),
                ("GITHUB_APP_ID", self.github_app_id),
            ):
                if not (plain or "").strip():
                    raise ConfigError(f"Missing required setting {name}")
        if github == "mock" and self.ahoy_env not in ("dev", "test"):
            raise ConfigError("--github mock is only allowed when AHOY_ENV is dev or test")

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ahoy import cli
from ahoy.app import create_app
from ahoy.config import ConfigError, Settings

CANARY = "canary-secret-value-9f3a"
SECRET_VARS = [
    "GEMINI_API_KEY",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_WEBHOOK_SECRET",
    "TOKEN_ENCRYPTION_KEY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*SECRET_VARS, "AHOY_ENV", "APP_ORIGIN"]:
        monkeypatch.delenv(name, raising=False)


def make(**kw: str) -> Settings:
    """Dev settings unless the test says otherwise (the real default is prod)."""
    return Settings(_env_file=None, **{"ahoy_env": "dev", **kw})  # type: ignore[arg-type]


def test_ahoy_env_defaults_to_prod() -> None:
    s = Settings(_env_file=None, app_origin="https://x.example")  # type: ignore[call-arg]
    assert s.ahoy_env == "prod"
    with pytest.raises(ConfigError, match="dev or test"):
        s.validate_for("mock", "mock")


def test_unset_ahoy_env_requires_https_origin() -> None:
    with pytest.raises((ConfigError, ValidationError), match="https"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_missing_gemini_key_fails() -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        create_app(make(), "gemini", "mock")


def test_missing_github_secret_fails() -> None:
    with pytest.raises(ConfigError, match="GITHUB_APP_PRIVATE_KEY"):
        create_app(make(gemini_api_key=CANARY), "mock", "real")


def test_mock_mock_needs_no_secrets() -> None:
    create_app(make(), "mock", "mock")


def test_github_mock_refused_in_prod() -> None:
    s = make(ahoy_env="prod", app_origin="https://x.example")
    with pytest.raises(ConfigError, match="dev or test"):
        s.validate_for("mock", "mock")


def test_cli_refuses_github_mock_in_prod(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AHOY_ENV", "prod")
    monkeypatch.setenv("APP_ORIGIN", "https://x.example")
    assert cli.main(["serve", "--provider", "mock", "--github", "mock"]) != 0
    assert "dev or test" in capsys.readouterr().err


def test_prod_http_origin_refused() -> None:
    with pytest.raises((ConfigError, ValidationError), match="https"):
        make(ahoy_env="prod", app_origin="http://x.example")


def test_health_fields_and_no_secrets() -> None:
    s = make(
        gemini_api_key=CANARY,
        github_app_private_key=CANARY,
        github_client_secret=CANARY,
        github_webhook_secret=CANARY,
        token_encryption_key=CANARY,
    )
    client = TestClient(create_app(s, "gemini", "real"))
    res = client.get("/api/health")
    assert res.status_code == 200
    assert set(res.json()) == {"provider", "model", "github"}
    assert res.json()["provider"] == "gemini"
    assert CANARY not in res.text
    assert CANARY not in repr(s)
    assert CANARY not in str(s)


def test_env_file_order_backend_overrides_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_env = tmp_path / "root.env"
    backend_env = tmp_path / "backend.env"
    root_env.write_text("GEMINI_MODEL=from-root\nREPO_CACHE_DIR=root-cache\n")
    backend_env.write_text("GEMINI_MODEL=from-backend\n")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("REPO_CACHE_DIR", raising=False)
    monkeypatch.setenv("AHOY_ENV", "dev")  # the real default is prod (https origin)
    s = Settings(_env_file=(root_env, backend_env))  # type: ignore[call-arg]
    assert s.gemini_model == "from-backend"
    assert s.repo_cache_dir == "root-cache"
    missing = tmp_path / "nope.env"
    assert Settings(_env_file=(missing, root_env)).gemini_model == "from-root"  # type: ignore[call-arg]


def test_settings_env_files_are_root_then_backend() -> None:
    files = Settings.model_config["env_file"]
    assert isinstance(files, tuple)
    root, backend = (Path(str(f)) for f in files)
    assert root.name == backend.name == ".env"
    assert backend.parent.name == "backend"
    assert root.parent == backend.parent.parent

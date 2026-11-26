import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ahoy import cli
from ahoy.app import create_app
from ahoy.config import ConfigError, Settings

CANARY = "canary-secret-value-9f3a"
# TOKEN_ENCRYPTION_KEY is parsed at startup, so its canary must be a valid 32-byte key.
KEY_CANARY = base64.urlsafe_b64encode(b"canary-token-key-0123456789abcde").decode()
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
        token_encryption_key=KEY_CANARY,
        github_client_id="Iv1.test",
        github_app_id="12345",
    )
    client = TestClient(create_app(s, "gemini", "real"))
    res = client.get("/api/health")
    assert res.status_code == 200
    assert set(res.json()) == {"provider", "model", "github"}
    assert res.json()["provider"] == "gemini"
    for secret in (CANARY, KEY_CANARY):
        assert secret not in res.text
        assert secret not in repr(s)
        assert secret not in str(s)


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


def test_empty_values_in_env_file_count_as_unset(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("AHOY_ENV=dev\nGIT_HISTORY_DEPTH=\nMAX_CONCURRENT_STEPS=\nGEMINI_MODEL=\n")
    s = Settings(_env_file=(env,))  # type: ignore[call-arg]
    assert s.git_history_depth == 20
    assert s.max_concurrent_steps == 8
    assert s.gemini_model


FAKE_PEM = f"-----BEGIN RSA PRIVATE KEY-----\n{CANARY}\n-----END RSA PRIVATE KEY-----\n"


def _pem(tmp_path: Path, text: str = FAKE_PEM, mode: int = 0o600) -> Path:
    f = tmp_path / "app.pem"
    f.write_text(text)
    f.chmod(mode)
    return f


def test_private_key_file_is_read(tmp_path: Path) -> None:
    s = make(github_app_private_key_file=str(_pem(tmp_path, mode=0o644)))
    value = s.github_app_private_key_value()
    assert value is not None
    assert value.get_secret_value() == FAKE_PEM.strip()


def test_private_key_file_and_value_both_set(tmp_path: Path) -> None:
    s = make(github_app_private_key_file=str(_pem(tmp_path)), github_app_private_key="x")
    with pytest.raises(ConfigError, match="GITHUB_APP_PRIVATE_KEY_FILE") as e:
        s.github_app_private_key_value()
    assert CANARY not in str(e.value)


def test_private_key_file_missing(tmp_path: Path) -> None:
    s = make(github_app_private_key_file=str(tmp_path / "nope.pem"))
    with pytest.raises(ConfigError, match="GITHUB_APP_PRIVATE_KEY_FILE"):
        s.github_app_private_key_value()


def test_private_key_file_not_pem(tmp_path: Path) -> None:
    s = make(github_app_private_key_file=str(_pem(tmp_path, CANARY)))
    with pytest.raises(ConfigError, match="PEM") as e:
        s.github_app_private_key_value()
    assert CANARY not in str(e.value)


def test_private_key_file_writable_refused(tmp_path: Path) -> None:
    s = make(github_app_private_key_file=str(_pem(tmp_path, mode=0o666)))
    with pytest.raises(ConfigError, match="writable") as e:
        s.github_app_private_key_value()
    assert CANARY not in str(e.value)


def test_validate_for_accepts_key_file_only(tmp_path: Path) -> None:
    s = make(
        github_app_private_key_file=str(_pem(tmp_path)),
        github_client_secret="a",
        github_webhook_secret="b",
        token_encryption_key="c",
        github_client_id="Iv1.test",
        github_app_id="12345",
    )
    s.validate_for("mock", "real")
    assert CANARY not in repr(s)
    assert CANARY not in str(s)


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:5173/",
        "http://127.0.0.1:5173/app",
        "http://127.0.0.1:5173?x=1",
        "http://127.0.0.1:5173#f",
        "http://user@127.0.0.1:5173",
        "ftp://127.0.0.1",
        "127.0.0.1:5173",
        "http://",
    ],
)
def test_app_origin_must_be_bare_origin(origin: str) -> None:
    with pytest.raises((ConfigError, ValidationError), match="APP_ORIGIN"):
        make(app_origin=origin)


def test_app_origin_bare_forms_accepted() -> None:
    make(app_origin="http://127.0.0.1:5173")
    make(app_origin="http://localhost")
    make(ahoy_env="prod", app_origin="https://ahoy.example")
    make(ahoy_env="prod", app_origin="https://[::1]:8443")


def test_trusted_proxy_wildcard_refused_in_prod() -> None:
    make(trusted_proxy_ips="*")  # dev: allowed
    make(ahoy_env="prod", app_origin="https://ahoy.example", trusted_proxy_ips="10.0.0.1")
    for value in ("*", "10.0.0.1, *"):
        with pytest.raises((ConfigError, ValidationError), match="TRUSTED_PROXY_IPS"):
            make(ahoy_env="prod", app_origin="https://ahoy.example", trusted_proxy_ips=value)


def test_ahoy_no_env_file_skips_env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ahoy import config

    env = tmp_path / ".env"
    env.write_text("ALLOWED_MODELS=from-file\nTRUSTED_PROXY_IPS=10.0.0.1\n")
    monkeypatch.setattr(config, "_ENV_FILES", (env,))
    for name in ("ALLOWED_MODELS", "TRUSTED_PROXY_IPS", "AHOY_NO_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AHOY_ENV", "dev")
    used = Settings()
    assert used.allowed_models == "from-file"
    assert used.trusted_proxy_ips == "10.0.0.1"
    monkeypatch.setenv("AHOY_NO_ENV_FILE", "1")
    ignored = Settings()
    assert ignored.allowed_models is None
    assert ignored.trusted_proxy_ips is None

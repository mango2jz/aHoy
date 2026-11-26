import pytest

from ahoy import cli
from ahoy.config import Settings

HINT = (
    "ahoy: hint: AHOY_ENV is unset, so it defaults to prod. For local development set AHOY_ENV=dev."
)
ARGV = ["serve", "--github", "mock", "--provider", "mock"]


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never read .env files, and never start a real server.
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))

    def no_server(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not start server")

    monkeypatch.setattr(cli.uvicorn, "run", no_server)


def test_hint_printed_when_ahoy_env_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AHOY_ENV", raising=False)
    assert cli.main(ARGV) == 2
    assert HINT in capsys.readouterr().err


def test_hint_absent_when_ahoy_env_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AHOY_ENV", "prod")
    assert cli.main(ARGV) == 2
    assert HINT not in capsys.readouterr().err

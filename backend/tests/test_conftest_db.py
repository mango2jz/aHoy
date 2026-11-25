"""The test-database setup when Postgres is unreachable: skip by default, fail with
AHOY_REQUIRE_DB=1. Runs one DB test in a child pytest against a closed port."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
CLOSED = "127.0.0.1:1"


def run_db_test(require_db: str | None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "TEST_MIGRATION_DATABASE_URL": f"postgresql+asyncpg://ahoy_owner:x@{CLOSED}/ahoy_test",
        "TEST_DATABASE_URL": f"postgresql+asyncpg://ahoy_app:x@{CLOSED}/ahoy_test",
    }
    env.pop("AHOY_REQUIRE_DB", None)
    if require_db is not None:
        env["AHOY_REQUIRE_DB"] = require_db
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/test_grants.py::test_connected_as_ahoy_app",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("value", [None, "0"])
def test_unreachable_postgres_skips(value: str | None) -> None:
    out = run_db_test(value)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 skipped" in out.stdout
    assert "Postgres unreachable at 127.0.0.1:1" in out.stdout


def test_unreachable_postgres_fails_when_required() -> None:
    out = run_db_test("1")
    assert out.returncode != 0
    assert "1 error" in out.stdout
    assert "AHOY_REQUIRE_DB=1: Postgres unreachable" in out.stdout


OWNER = "postgresql+asyncpg://ahoy_owner:x@127.0.0.1:5432/ahoy_test"
APP = "postgresql+asyncpg://ahoy_app:x@127.0.0.1:5432/ahoy_test"


@pytest.mark.parametrize(
    ("owner", "app", "message"),
    [
        (OWNER.replace("ahoy_test", "ahoy"), APP.replace("ahoy_test", "ahoy"), "_test database"),
        (OWNER, APP.replace("ahoy_test", "other_test"), "different databases"),
        (OWNER + "?database=ahoy", APP, "database as query"),
        (OWNER, APP + "?dbname=ahoy", "dbname as query"),
        (OWNER + "?host=db.prod&port=6543", APP, "host, port as query"),
        (OWNER, APP.replace("127.0.0.1:5432", "127.0.0.1:5433"), "different servers"),
        (OWNER, APP.replace("127.0.0.1", "db.prod"), "different servers"),
    ],
)
def test_test_database_urls_are_checked(
    monkeypatch: pytest.MonkeyPatch, owner: str, app: str, message: str
) -> None:
    # The conftest drops this database, so URLs that could aim elsewhere are refused.
    from conftest import _test_urls

    monkeypatch.setenv("TEST_MIGRATION_DATABASE_URL", owner)
    monkeypatch.setenv("TEST_DATABASE_URL", app)
    with pytest.raises(pytest.UsageError, match=message):
        _test_urls()

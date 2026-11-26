"""T9a: GET /api/sessions/{id}/files (SPEC -> API, Privacy and safety, Tools -> read_file).

Most cases read a synthetic snapshot seeded into the repo cache under the session's
(repo_id, sha); the re-fetch case uses the real fixture/demo snapshot from mock GitHub.
"""

import asyncio
import shutil
import subprocess
import tempfile
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.api import files as files_api
from ahoy.github.app_client import RepoTarget, SnapshotError, _git_env, remove_snapshot
from ahoy.github.mock import INSTALLATION_ID
from ahoy.github.repo_cache import RepoCache
from ahoy.harness.schemas import Session
from ahoy.store import repo
from ahoy.tools.fs import LINE_CLIP_MARKER, MAX_LINE_CHARS
from conftest import MakeSession
from test_api_repos import gh_of, make_api_app, signed_in
from test_api_sessions import user_of

pytestmark = pytest.mark.db

DEMO = 7001
CANARY = "PEM_CANARY_DO_NOT_SERVE"


@pytest.fixture
def app(db: AsyncSession, tmp_path: Path) -> FastAPI:
    return make_api_app(db, tmp_path / "cache")


def build_snapshot(dest: Path, outside: Path) -> None:
    (dest / "src").mkdir()
    (dest / "src" / "app.py").write_text("".join(f"line {n}\n" for n in range(1, 31)))
    (dest / "long.txt").write_text("x" * 5000 + "\nshort\n")
    (dest / "big.txt").write_text(("y" * 199 + "\n") * 500)  # ~100 KB > 64 KiB cap
    (dest / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (dest / "bin.dat").write_bytes(b"abc\x00def\n")
    (dest / "a b.txt").write_text("hello\nworld\n")
    (dest / "empty.txt").write_text("")
    (dest / "dir").mkdir()
    (dest / "dir" / "inner.txt").write_text("inner\n")
    (dest / ".git").mkdir()
    (dest / ".git" / "config").write_text("[core]\n")
    (dest / "config").mkdir()
    (dest / "config" / "dev.pem").write_text(f"{CANARY}\n")
    (dest / ".env").write_text(f"{CANARY}\n")
    (dest / "link_out").symlink_to(outside)
    (dest / "link_in").symlink_to("src/app.py")
    (dest / "link_env").symlink_to(".env")
    (dest / "link_git").symlink_to(".git")


async def seed(app: FastAPI, session: Session, tmp_path: Path) -> None:
    """Put the synthetic snapshot in the cache under the session's key."""
    outside = tmp_path / "outside.txt"
    outside.write_text(f"{CANARY}\n")

    async def fetch(dest: Path) -> None:
        build_snapshot(dest, outside)

    cache: RepoCache = app.state.repo_cache
    async with cache.lease(session.repo_id, session.commit_sha, fetch):
        pass


async def get(client: httpx.AsyncClient, session: Session, **params: Any) -> httpx.Response:
    return await client.get(f"/api/sessions/{session.id}/files", params=params)


async def owner_setup(
    stack: AsyncExitStack,
    app: FastAPI,
    db: AsyncSession,
    make_session: MakeSession,
    tmp_path: Path,
    *,
    shared: bool = False,
) -> tuple[httpx.AsyncClient, Session]:
    alice = await signed_in(stack, app, "alice")
    session = await make_session(await user_of(db, alice), shared=shared)
    await seed(app, session, tmp_path)
    return alice, session


def reason(resp: httpx.Response) -> str:
    detail: dict[str, str] = resp.json()["detail"]
    return detail["reason"]


# --- access ------------------------------------------------------------------------------


async def test_owner_reads_lines(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        resp = await get(alice, session, path="src/app.py")
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "private, no-store"
        body = resp.json()
        assert body["path"] == "src/app.py"
        assert body["start_line"] == 1 and body["end_line"] == 30
        assert body["total_lines"] == 30
        assert body["lines"] == [f"line {n}" for n in range(1, 31)]
        assert body["clipped_lines"] == 0 and body["truncated"] is False
        assert body["github_url"] == (
            f"https://github.com/fixture/demo/blob/{session.commit_sha}/src/app.py#L1-L30"
        )
        # The lease is held only during the read.
        cache: RepoCache = app.state.repo_cache
        assert cache.leases_of(session.repo_id, session.commit_sha) == 0


async def test_shared_viewer_reads_stranger_404(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        _alice, shared = await owner_setup(stack, app, db, make_session, tmp_path, shared=True)
        bob = await signed_in(stack, app, "bob")
        mallory = await signed_in(stack, app, "mallory")
        resp = await get(bob, shared, path="src/app.py")
        assert resp.status_code == 200 and resp.json()["lines"][0] == "line 1"
        resp = await get(mallory, shared, path="src/app.py")
        assert resp.status_code == 404 and "line 1" not in resp.text


async def test_not_shared_viewer_404(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        _alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        bob = await signed_in(stack, app, "bob")
        assert (await get(bob, session, path="src/app.py")).status_code == 404


async def test_not_ready_409(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        for status in ("preparing", "failed"):
            await repo.set_session_status(db, session.id, status)
            resp = await get(alice, session, path="src/app.py")
            assert resp.status_code == 409 and reason(resp) == "session_not_ready"


# --- path resolution ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "nope.txt",
        "../outside.txt",
        "src/../../outside.txt",
        "/etc/passwd",
        "link_out",
        ".git/config",
        ".GIT/config",
        "link_git/config",
        "src/app.py/x",
    ],
)
async def test_outside_or_missing_is_one_404(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path, path: str
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        resp = await get(alice, session, path=path)
        assert resp.status_code == 404
        assert resp.json() == {"detail": {"reason": "file_not_found"}}
        assert CANARY not in resp.text


@pytest.mark.parametrize(
    "path", [".env", ".ENV", "config/dev.pem", "deploy/id_ed25519", "link_env"]
)
async def test_denylisted_403_without_content(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path, path: str
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        resp = await get(alice, session, path=path)
        assert resp.status_code == 403
        assert resp.json() == {"detail": {"reason": "denylisted"}}
        assert CANARY not in resp.text


async def test_symlink_inside_is_followed(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        resp = await get(alice, session, path="link_in", end=2)
        assert resp.status_code == 200
        assert resp.json()["path"] == "src/app.py"
        assert resp.json()["lines"] == ["line 1", "line 2"]


async def test_directory_and_binary_422(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        for path in ("dir", "", "."):
            resp = await get(alice, session, path=path)
            assert resp.status_code == 422 and reason(resp) == "not_a_file", path
        resp = await get(alice, session, path="bin.dat")
        assert resp.status_code == 422 and reason(resp) == "binary_file"
        assert "abc" not in resp.text


# --- ranges and caps ---------------------------------------------------------------------


async def test_range(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        body = (await get(alice, session, path="src/app.py", start=5, end=7)).json()
        assert body["lines"] == ["line 5", "line 6", "line 7"]
        assert (body["start_line"], body["end_line"], body["total_lines"]) == (5, 7, 30)
        assert body["truncated"] is False
        assert body["github_url"].endswith("/src/app.py#L5-L7")
        # end past the file: what exists, not truncated
        body = (await get(alice, session, path="src/app.py", start=28, end=100)).json()
        assert body["lines"] == ["line 28", "line 29", "line 30"] and body["end_line"] == 30
        assert body["truncated"] is False
        # start only: end defaults to start + 499
        body = (await get(alice, session, path="src/app.py", start=30)).json()
        assert body["lines"] == ["line 30"]
        # exactly 500 lines is allowed
        assert (await get(alice, session, path="src/app.py", start=1, end=500)).status_code == 200
        for params in (
            {"start": 0},
            {"start": -1, "end": 3},
            {"start": 5, "end": 4},
            {"start": 1, "end": 501},
            {"start": 31},  # past the end of a 30-line file
        ):
            resp = await get(alice, session, path="src/app.py", **params)
            assert resp.status_code == 422 and reason(resp) == "bad_range", params


async def test_line_endings_and_empty_file(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        body = (await get(alice, session, path="crlf.txt")).json()
        assert body["lines"] == ["one", "two"] and body["end_line"] == 2
        body = (await get(alice, session, path="empty.txt")).json()
        assert body["lines"] == [] and body["total_lines"] == 0 and body["truncated"] is False
        resp = await get(alice, session, path="empty.txt", start=2)
        assert resp.status_code == 422 and reason(resp) == "bad_range"


async def test_long_lines_clipped(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        body = (await get(alice, session, path="long.txt")).json()
        assert body["lines"] == ["x" * MAX_LINE_CHARS + LINE_CLIP_MARKER, "short"]
        assert body["clipped_lines"] == 1
        body = (await get(alice, session, path="long.txt", start=2)).json()
        assert body["clipped_lines"] == 0


async def test_byte_cap_truncates(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        body = (await get(alice, session, path="big.txt")).json()
        assert body["truncated"] is True and body["total_lines"] == 500
        assert 0 < len(body["lines"]) < 500
        assert body["end_line"] == len(body["lines"])
        assert sum(len(line) + 1 for line in body["lines"]) <= 64 * 1024
        assert body["github_url"].endswith(f"#L1-L{body['end_line']}")
        # a small range inside the cap is not truncated
        body = (await get(alice, session, path="big.txt", start=10, end=20)).json()
        assert body["truncated"] is False and len(body["lines"]) == 11


async def test_github_url_quotes_path(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, tmp_path: Path
) -> None:
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        body = (await get(alice, session, path="a b.txt")).json()
        assert body["github_url"] == (
            f"https://github.com/fixture/demo/blob/{session.commit_sha}/a%20b.txt#L1-L2"
        )


# --- the real snapshot: evicted, then re-fetched ----------------------------------------


def head_sha(app: FastAPI) -> str:
    git = shutil.which("git") or "git"
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run(  # noqa: S603
            [git, "rev-parse", "main"],
            cwd=gh_of(app).bare_repo(DEMO),
            env=_git_env(home),
            check=True,
            capture_output=True,
        )
    return out.stdout.decode().strip()


async def test_evicted_snapshot_is_refetched(app: FastAPI, db: AsyncSession) -> None:
    gh = gh_of(app)
    fetches = 0
    real = gh.fetch_snapshot

    async def counting(target: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        nonlocal fetches
        fetches += 1
        await real(target, sha, dest, depth)

    gh.fetch_snapshot = counting  # type: ignore[method-assign]
    sha = head_sha(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        session, _root = await repo.create_session(
            db,
            owner_id=(await user_of(db, alice)).id,
            installation_id=INSTALLATION_ID,
            repo_id=DEMO,
            repo_full_name="fixture/demo",
            ref="main",
            commit_sha=sha,
            private=True,
            model="mock",
            status="ready",
        )

        resp = await get(alice, session, path="README.md", end=3)
        assert resp.status_code == 200, resp.text
        assert fetches == 1
        first = resp.json()
        # Bait is stripped from the snapshot and refused by name anyway.
        for path in (".env", "config/dev.pem", "deploy/id_ed25519"):
            assert (await get(alice, session, path=path)).status_code == 403

        cache: RepoCache = app.state.repo_cache
        remove_snapshot(cache.path_for(DEMO, sha))  # evicted behind the session's back
        resp = await get(alice, session, path="README.md", end=3)
        assert resp.status_code == 200 and resp.json() == first
        assert fetches == 2


# --- lease lifetime and fetch failures ---------------------------------------------------


async def test_cancelled_request_keeps_lease_until_read_ends(
    app: FastAPI,
    db: AsyncSession,
    make_session: MakeSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = files_api._read

    def blocking(root: Path, path: str, start: int) -> Any:
        entered.set()
        release.wait(10)
        return real(root, path, start)

    monkeypatch.setattr(files_api, "_read", blocking)
    cache: RepoCache = app.state.repo_cache
    async with AsyncExitStack() as stack:
        alice, session = await owner_setup(stack, app, db, make_session, tmp_path)
        key = (session.repo_id, session.commit_sha)
        request = asyncio.create_task(get(alice, session, path="src/app.py"))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            request.cancel()
            await asyncio.sleep(0.05)
            assert not request.done()  # still waiting for the thread
            assert cache.leases_of(*key) == 1
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert cache.leases_of(*key) == 0


@pytest.mark.parametrize(
    "error", [OSError("disk /secret/path full"), SnapshotError("fetch failed /secret/path")]
)
async def test_fetch_failure_is_503(
    app: FastAPI,
    db: AsyncSession,
    make_session: MakeSession,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    async def broken(target: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        raise error

    gh_of(app).fetch_snapshot = broken  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        session = await make_session(await user_of(db, alice))  # not cached: must fetch
        resp = await get(alice, session, path="README.md")
        assert resp.status_code == 503 and reason(resp) == "snapshot_unavailable"
        assert "/secret/path" not in resp.text and "/secret/path" not in caplog.text
        assert type(error).__name__ in caplog.text
        cache: RepoCache = app.state.repo_cache
        assert cache.leases_of(session.repo_id, session.commit_sha) == 0

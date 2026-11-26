"""T23b: GET /api/repos (60 s per-user cache, install URL) and the private-repo notice ack."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.api.repos import RepoListCache
from ahoy.app import create_app
from ahoy.auth import crypto, sessions
from ahoy.auth.sessions import AuthState
from ahoy.config import Settings
from ahoy.github.http import GitHubHTTPError, GitHubRateLimitedError
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.store import auth as auth_store
from conftest import SameSession

pytestmark = pytest.mark.db

ORIGIN = "http://127.0.0.1:5173"
CSRF = {"Origin": ORIGIN, "X-Ahoy-CSRF": "1"}
SLUG = "ahoy-test-app"


def make_api_app(db: AsyncSession, cache_dir: Path) -> FastAPI:
    """The real app (mock provider + mock GitHub) bound to the test's DB session."""
    settings = Settings(
        _env_file=None,
        ahoy_env="test",
        database_url=None,
        app_origin=ORIGIN,
        token_encryption_key=crypto.generate_key(),
        github_app_slug=SLUG,
        repo_cache_dir=str(cache_dir),
    )
    app = create_app(settings, "mock", "mock")

    async def override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[sessions.get_db] = override
    auth: AuthState = app.state.auth
    auth.sessionmaker = SameSession(db).maker()
    return app


async def signed_in(stack: AsyncExitStack, app: FastAPI, name: str) -> httpx.AsyncClient:
    client = await stack.enter_async_context(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    )
    resp = await client.get("/auth/mock/login", params={"user": name})
    assert resp.status_code == 303, resp.text
    return client


def gh_of(app: FastAPI) -> MockGitHub:
    return cast(MockGitHub, app.state.github_user)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def app(db: AsyncSession, tmp_path: Path) -> FastAPI:
    return make_api_app(db, tmp_path / "cache")


async def test_lists_installed_repos_and_install_url(app: FastAPI) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        resp = await alice.get("/api/repos")
        assert resp.status_code == 200
        body = resp.json()
        assert body["install_url"] == f"https://github.com/apps/{SLUG}/installations/new"
        repos = {r["id"]: r for r in body["repos"]}
        assert set(repos) == {7001, 7002}
        assert repos[7001] == {
            "id": 7001,
            "full_name": "fixture/demo",
            "private": True,
            "default_branch": "main",
            "installation_id": INSTALLATION_ID,
        }
        assert repos[7002]["private"] is False
        # mallory can read no repo of the installation: nothing listed
        mallory = await signed_in(stack, app, "mallory")
        assert (await mallory.get("/api/repos")).json()["repos"] == []


async def test_listing_cached_60s_per_user(app: FastAPI) -> None:
    gh = gh_of(app)
    calls: list[str] = []
    real = gh.list_installations

    async def counting(token: Any) -> Any:
        calls.append("x")
        return await real(token)

    gh.list_installations = counting  # type: ignore[method-assign]
    clock = Clock()
    cache: RepoListCache = app.state.repo_list
    cache.clock = clock
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        bob = await signed_in(stack, app, "bob")
        await alice.get("/api/repos")
        await alice.get("/api/repos")
        assert len(calls) == 1
        await bob.get("/api/repos")  # per user
        assert len(calls) == 2
        gh.revoke_access("alice", 7002)
        clock.t += 59
        ids = {r["id"] for r in (await alice.get("/api/repos")).json()["repos"]}
        assert ids == {7001, 7002} and len(calls) == 2  # still cached
        clock.t += 2
        ids = {r["id"] for r in (await alice.get("/api/repos")).json()["repos"]}
        assert ids == {7001} and len(calls) == 3


async def test_sign_in_drops_the_cached_listing(app: FastAPI) -> None:
    gh = gh_of(app)
    gh.remove_from_installation(7001)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        ids = {r["id"] for r in (await alice.get("/api/repos")).json()["repos"]}
        assert ids == {7002}
        gh.add_to_installation(7001)  # the user just added the repo to the App
        ids = {r["id"] for r in (await alice.get("/api/repos")).json()["repos"]}
        assert ids == {7002}  # cached
        assert (await alice.get("/auth/mock/login", params={"user": "alice"})).status_code == 303
        ids = {r["id"] for r in (await alice.get("/api/repos")).json()["repos"]}
        assert ids == {7001, 7002}


async def test_listing_cache_is_bounded(app: FastAPI) -> None:
    cache: RepoListCache = app.state.repo_list
    cache._max = 1
    async with AsyncExitStack() as stack:
        for name in ("alice", "bob"):
            client = await signed_in(stack, app, name)
            assert (await client.get("/api/repos")).status_code == 200
    assert len(cache) == 1


@pytest.mark.parametrize(
    "error",
    [
        GitHubRateLimitedError("rate limited", status=403, retry_after=60),
        GitHubHTTPError("boom", status=502),
    ],
)
async def test_github_errors_are_503_and_not_cached(app: FastAPI, error: Exception) -> None:
    gh = gh_of(app)
    real = gh.list_installations

    async def failing(token: Any) -> Any:
        raise error

    gh.list_installations = failing  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        assert (await alice.get("/api/repos")).status_code == 503
        gh.list_installations = real  # type: ignore[method-assign]
        resp = await alice.get("/api/repos")  # the failure was not cached as "no repos"
        assert resp.status_code == 200
        assert {r["id"] for r in resp.json()["repos"]} == {7001, 7002}


async def test_suspended_installation_skipped(app: FastAPI) -> None:
    gh = gh_of(app)
    real = gh.list_installations

    async def suspended(token: Any) -> Any:
        return [i.model_copy(update={"suspended": True}) for i in await real(token)]

    gh.list_installations = suspended  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        assert (await alice.get("/api/repos")).json()["repos"] == []


async def test_private_repo_notice_ack(app: FastAPI, db: AsyncSession) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        me = (await alice.get("/api/me")).json()
        assert me["private_repo_notice_ack"] is False
        for headers in ({}, {"Origin": ORIGIN}, {"X-Ahoy-CSRF": "1"}):
            resp = await alice.post("/api/me/private-repo-notice", headers=headers)
            assert resp.status_code == 403
        assert (await alice.get("/api/me")).json()["private_repo_notice_ack"] is False
        resp = await alice.post("/api/me/private-repo-notice", headers=CSRF)
        assert resp.status_code == 204
        assert (await alice.get("/api/me")).json()["private_repo_notice_ack"] is True
        user = await auth_store.get_user(db, UUID(me["id"]))
        assert user is not None and user.private_repo_notice_ack_at is not None
        first = user.private_repo_notice_ack_at
        # idempotent: the first acknowledgement's time is kept
        assert (await alice.post("/api/me/private-repo-notice", headers=CSRF)).status_code == 204
        again = await auth_store.get_user(db, UUID(me["id"]))
        assert again is not None and again.private_repo_notice_ack_at == first
        # only alice acknowledged
        bob = await signed_in(stack, app, "bob")
        assert (await bob.get("/api/me")).json()["private_repo_notice_ack"] is False


async def test_signed_out_is_401(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/repos")).status_code == 401
        resp = await client.post("/api/me/private-repo-notice", headers=CSRF)
        assert resp.status_code == 401


async def test_unusable_token_401_clears_cookie(app: FastAPI, db: AsyncSession) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        user = await auth_store.get_user(db, UUID((await alice.get("/api/me")).json()["id"]))
    assert user is not None
    await auth_store.delete_user_auth(db, user.id)  # tokens gone, e.g. refresh refused
    cache: RepoListCache = app.state.repo_list
    with pytest.raises(HTTPException) as info:
        await cache.repos(app.state.auth, db, user)
    assert info.value.status_code == 401
    assert (info.value.headers or {})["set-cookie"].startswith("ahoy_sid=")

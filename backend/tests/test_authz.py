"""T22: authorize(), load_session() and the repo-access cache (SPEC -> Authorization, D14).

Runs against mock GitHub: alice and bob can read the private fixture/demo (7001), mallory
cannot. Spies count the GitHub calls so "no GitHub call before 404" is checked, not assumed.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy import authz
from ahoy.auth import crypto, sessions
from ahoy.auth.sessions import AuthState
from ahoy.authz import AccessCache, Action
from ahoy.config import Settings
from ahoy.github.http import GitHubHTTPError, GitHubRateLimitedError
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.harness.schemas import Session, User
from ahoy.store import auth as auth_store
from conftest import MakeSession, SameSession, SignUp

DEMO = 7001  # private, alice + bob
PUBLIC = 7002
READS = (Action.VIEW, Action.READ_FILES, Action.SUBSCRIBE)
WRITES = (Action.STEER, Action.ALLOW_BLOCKED, Action.TOGGLE_SHARE, Action.DELETE)


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class Spy:
    """Counts can_read_repo / can_mint_for calls on a MockGitHub (and can make them fail)."""

    def __init__(self, gh: MockGitHub) -> None:
        self.read = 0
        self.mint = 0
        self.error: Exception | None = None
        read, mint = gh.can_read_repo, gh.can_mint_for

        async def can_read_repo(token: SecretStr, repo_id: int) -> bool:
            self.read += 1
            if self.error is not None:
                raise self.error
            return await read(token, repo_id)

        async def can_mint_for(installation_id: int, repo_id: int) -> bool:
            self.mint += 1
            return await mint(installation_id, repo_id)

        gh.can_read_repo = can_read_repo  # type: ignore[method-assign]
        gh.can_mint_for = can_mint_for  # type: ignore[method-assign]

    @property
    def calls(self) -> int:
        return self.read + self.mint


class Env:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.clock = Clock()
        self.gh = MockGitHub(ahoy_env="test")
        self.auth = AuthState(
            settings=Settings(_env_file=None, ahoy_env="test", database_url=None),
            key=crypto.generate_key(),
            github=self.gh,
            default_model="mock",
            sessionmaker=SameSession(db).maker(),
            clock=self.clock,
        )
        self.cache = AccessCache(self.gh, self.gh, clock=self.auth.now)
        self.auth.access = self.cache
        self.spy = Spy(self.gh)
        self.users: dict[str, User] = {}

    async def load(
        self, name: str, session: Session, action: Action = Action.VIEW, **kw: Any
    ) -> Session:
        return await authz.load_session(
            self.auth, self.db, self.users[name], session.id, action, **kw
        )

    async def status(self, name: str, session: Session, action: Action = Action.VIEW) -> int:
        try:
            await self.load(name, session, action)
        except HTTPException as exc:
            return exc.status_code
        return 200


pytestmark = pytest.mark.db


@pytest.fixture
async def env(db: AsyncSession, sign_up: SignUp) -> Env:
    e = Env(db)
    for name in ("alice", "bob", "mallory"):
        e.users[name] = await sign_up(e.auth, name)
    return e


# --- the table ------------------------------------------------------------------------


@pytest.mark.parametrize("action", list(Action))
async def test_owner_allowed_for_every_action(
    env: Env, make_session: MakeSession, action: Action
) -> None:
    session = await make_session(env.users["alice"])
    loaded = await env.load("alice", session, action)
    assert loaded.id == session.id
    assert env.spy.calls == (0 if action is Action.DELETE else 2)


@pytest.mark.parametrize("action", READS)
async def test_shared_viewer_reads(env: Env, make_session: MakeSession, action: Action) -> None:
    session = await make_session(env.users["alice"], shared=True)
    assert await env.status("bob", session, action) == 200


@pytest.mark.parametrize("action", WRITES)
async def test_shared_viewer_write_403_after_access_check(
    env: Env, make_session: MakeSession, action: Action
) -> None:
    session = await make_session(env.users["alice"], shared=True)
    assert await env.status("bob", session, action) == 403
    assert env.spy.calls == 2  # access first, so the 403 never reveals a hidden session


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("name", ["bob", "mallory"])
async def test_not_shared_404_before_any_github_call(
    env: Env, make_session: MakeSession, action: Action, name: str
) -> None:
    session = await make_session(env.users["alice"])
    assert await env.status(name, session, action) == 404
    assert env.spy.calls == 0


@pytest.mark.parametrize("action", list(Action))
async def test_shared_but_no_repo_access_404(
    env: Env, make_session: MakeSession, action: Action
) -> None:
    session = await make_session(env.users["alice"], shared=True)
    assert await env.status("mallory", session, action) == 404


async def test_shared_public_repo_visible_to_any_signed_in_user(
    env: Env, make_session: MakeSession
) -> None:
    session = await make_session(env.users["alice"], repo_id=PUBLIC, shared=True)
    assert await env.status("mallory", session) == 200
    env.gh.remove_from_installation(PUBLIC)  # part 2 still hides it
    env.cache.invalidate(PUBLIC)
    assert await env.status("mallory", session) == 404


async def test_missing_session_404(env: Env) -> None:
    with pytest.raises(HTTPException) as info:
        await authz.load_session(env.auth, env.db, env.users["alice"], uuid4(), Action.DELETE)
    assert info.value.status_code == 404
    assert env.spy.calls == 0


async def test_check_repo_access_shares_the_session_cache_entry(
    env: Env, make_session: MakeSession
) -> None:
    """Session creation asks by (repo, installation) before a session exists; a later
    load_session for that repo reuses the same cached answer."""
    alice, mallory = env.users["alice"], env.users["mallory"]
    assert await authz.check_repo_access(env.auth, env.db, alice, DEMO, INSTALLATION_ID)
    assert env.spy.calls == 2
    await env.load("alice", await make_session(alice))
    assert env.spy.calls == 2
    assert not await authz.check_repo_access(env.auth, env.db, mallory, DEMO, INSTALLATION_ID)


async def test_owner_losing_access_hides_but_can_delete(
    env: Env, make_session: MakeSession
) -> None:
    session = await make_session(env.users["alice"])
    env.gh.revoke_access("alice", DEMO)
    for action in (*READS, Action.STEER, Action.TOGGLE_SHARE):
        assert await env.status("alice", session, action) == 404
    assert await env.status("alice", session, Action.DELETE) == 200
    env.gh.restore_access("alice", DEMO)
    env.cache.invalidate_user(env.users["alice"].id)
    assert await env.status("alice", session) == 200  # reappears


async def test_load_session_accepts_request(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"])
    app = SimpleNamespace(state=SimpleNamespace(auth=env.auth))
    request = Request({"type": "http", "app": app})
    loaded = await authz.load_session(request, env.db, env.users["alice"], session.id, Action.VIEW)
    assert loaded == session


async def test_authorize_without_cache_503(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"])
    env.auth.access = None
    with pytest.raises(HTTPException) as info:
        await authz.authorize(env.auth, env.db, env.users["alice"], session, Action.VIEW)
    assert info.value.status_code == 503


async def test_refused_token_401_clears_cookie(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"], shared=True)
    await auth_store.delete_user_auth(env.db, env.users["bob"].id)  # tokens gone
    with pytest.raises(HTTPException) as info:
        await env.load("bob", session)
    assert info.value.status_code == 401
    cookie = (info.value.headers or {})["set-cookie"]
    assert cookie.startswith(f"{sessions.SESSION_COOKIE_DEV}=") and "Max-Age=0" in cookie


async def test_github_down_during_refresh_503_keeps_cookie(
    env: Env, make_session: MakeSession
) -> None:
    session = await make_session(env.users["alice"], shared=True)

    async def down(refresh_token: SecretStr) -> Any:
        raise GitHubHTTPError("down", status=502)

    env.gh.refresh = down  # type: ignore[method-assign]
    env.clock.advance(8 * 3600 - 60)  # access token near expiry: refresh needed
    with pytest.raises(HTTPException) as info:
        await env.load("bob", session)
    assert info.value.status_code == 503
    assert "set-cookie" not in (info.value.headers or {})


# --- revocation (success criterion 9) -------------------------------------------------


async def test_revocation_hidden_after_ttl_not_before(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"], shared=True)
    assert await env.status("bob", session) == 200
    env.gh.revoke_access("bob", DEMO)
    env.clock.advance(299)
    assert await env.status("bob", session) == 200  # cached
    env.clock.advance(1)
    assert await env.status("bob", session) == 404


async def test_revocation_by_installation_removal_after_ttl(
    env: Env, make_session: MakeSession
) -> None:
    session = await make_session(env.users["alice"], shared=True)
    assert await env.status("bob", session) == 200
    env.gh.remove_from_installation(DEMO)
    assert await env.status("bob", session) == 200
    env.clock.advance(301)
    assert await env.status("bob", session) == 404
    env.gh.add_to_installation(DEMO)
    env.clock.advance(301)  # the "no" was cached too
    assert await env.status("bob", session) == 200


async def test_fresh_sees_revocation_and_refreshes_entry(
    env: Env, make_session: MakeSession
) -> None:
    session = await make_session(env.users["alice"], shared=True)
    await env.load("bob", session)
    env.gh.revoke_access("bob", DEMO)
    with pytest.raises(HTTPException) as info:
        await env.load("bob", session, fresh=True)
    assert info.value.status_code == 404
    assert await env.status("bob", session) == 404  # the refreshed entry is used


async def test_invalidate_repo_is_immediate(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"], shared=True)
    await env.load("bob", session)
    await env.load("alice", session)
    env.gh.revoke_access("bob", DEMO)
    env.cache.invalidate(DEMO)
    assert len(env.cache) == 0
    assert await env.status("bob", session) == 404


async def test_invalidate_user_drops_only_that_user(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"], shared=True)
    await env.load("bob", session)
    await env.load("alice", session)
    env.cache.invalidate_user(env.users["bob"].id)
    assert len(env.cache) == 1
    calls = env.spy.calls
    await env.load("alice", session)
    assert env.spy.calls == calls


async def test_invalidate_installation_drops_only_that_installation(
    env: Env, make_session: MakeSession
) -> None:
    good = await make_session(env.users["alice"])
    other = await make_session(env.users["alice"], installation_id=INSTALLATION_ID + 1)
    await env.load("alice", good)
    assert await env.status("alice", other) == 404
    assert len(env.cache) == 2
    env.cache.invalidate_installation(INSTALLATION_ID + 1)
    assert len(env.cache) == 1
    calls = env.spy.calls
    await env.load("alice", good)
    assert env.spy.calls == calls
    env.cache.invalidate_installation(INSTALLATION_ID)
    assert len(env.cache) == 0


# --- the cache ------------------------------------------------------------------------


async def test_cache_key_includes_installation(env: Env, make_session: MakeSession) -> None:
    good = await make_session(env.users["alice"])
    other = await make_session(env.users["alice"], installation_id=INSTALLATION_ID + 1)
    assert await env.status("alice", good) == 200
    assert await env.status("alice", other) == 404  # that installation cannot mint
    assert await env.status("alice", good) == 200


async def test_hits_and_misses_count_github_calls(env: Env, make_session: MakeSession) -> None:
    session = await make_session(env.users["alice"], shared=True)
    await env.load("bob", session)
    await env.load("bob", session)
    assert (env.spy.read, env.spy.mint) == (1, 1)
    assert await env.status("mallory", session) == 404
    assert (env.spy.read, env.spy.mint) == (2, 1)  # no user access: mint not asked
    assert await env.status("mallory", session) == 404
    assert env.spy.read == 2  # "no" is cached


@pytest.mark.parametrize(
    "error",
    [
        GitHubRateLimitedError("rate limited", status=403, retry_after=60),
        GitHubHTTPError("server error", status=502),
    ],
)
async def test_github_errors_503_and_not_cached(
    env: Env, make_session: MakeSession, error: Exception
) -> None:
    session = await make_session(env.users["alice"], shared=True)
    env.spy.error = error
    assert await env.status("bob", session) == 503
    assert len(env.cache) == 0
    env.spy.error = None
    assert await env.status("bob", session) == 200


class Gate:
    """A GitHub stand-in whose answers wait for `release`."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.answer = True

    async def can_read_repo(self, token: SecretStr, repo_id: int) -> bool:
        self.calls += 1
        await self.release.wait()
        return self.answer

    async def can_mint_for(self, installation_id: int, repo_id: int) -> bool:
        return True


def _gate_cache(clock: Clock, **kw: Any) -> tuple[AccessCache, Gate]:
    gate = Gate()
    return AccessCache(gate, gate, clock=clock, **kw), gate  # type: ignore[arg-type]


TOKEN = SecretStr("ghu_test")


async def test_concurrent_misses_share_one_check() -> None:
    cache, gate = _gate_cache(Clock())
    key = (uuid4(), DEMO, INSTALLATION_ID)
    tasks = [asyncio.ensure_future(cache.check(key, TOKEN)) for _ in range(5)]
    await asyncio.sleep(0)
    gate.release.set()
    assert await asyncio.gather(*tasks) == [True] * 5
    assert gate.calls == 1
    assert cache.cached(key) is True


async def test_cancelled_waiter_does_not_cancel_shared_check() -> None:
    cache, gate = _gate_cache(Clock())
    key = (uuid4(), DEMO, INSTALLATION_ID)
    first = asyncio.ensure_future(cache.check(key, TOKEN))
    second = asyncio.ensure_future(cache.check(key, TOKEN))
    await asyncio.sleep(0)
    first.cancel()
    gate.release.set()
    assert await second is True
    assert first.cancelled()


async def test_failed_check_with_no_waiters_is_marked_seen() -> None:
    class Boom(Gate):
        async def can_read_repo(self, token: SecretStr, repo_id: int) -> bool:
            await self.release.wait()
            raise GitHubHTTPError("down", status=500)

    gate = Boom()
    cache = AccessCache(gate, gate, clock=Clock())  # type: ignore[arg-type]
    key = (uuid4(), DEMO, INSTALLATION_ID)
    waiter = asyncio.ensure_future(cache.check(key, TOKEN))
    await asyncio.sleep(0)
    waiter.cancel()
    gate.release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert cache.cached(key) is None
    assert not cache._inflight


class Stepped:
    """A GitHub stand-in: call i answers `answers[i]` once `events[i]` is set."""

    def __init__(self, *answers: bool) -> None:
        self.answers = answers
        self.events: list[asyncio.Event] = []

    async def can_read_repo(self, token: SecretStr, repo_id: int) -> bool:
        call = len(self.events)
        self.events.append(asyncio.Event())
        await self.events[call].wait()
        return self.answers[call]

    async def can_mint_for(self, installation_id: int, repo_id: int) -> bool:
        return True


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_invalidation_during_check_wins() -> None:
    gate = Stepped(True, False)
    cache = AccessCache(gate, gate, clock=Clock())  # type: ignore[arg-type]
    user = uuid4()
    key = (user, DEMO, INSTALLATION_ID)
    old = asyncio.ensure_future(cache.check(key, TOKEN))
    await _settle()
    cache.invalidate_user(user)  # e.g. a webhook while GitHub is answering
    new = asyncio.ensure_future(cache.check(key, TOKEN))  # starts its own check
    await _settle()
    assert len(gate.events) == 2
    gate.events[1].set()
    assert await new is False
    gate.events[0].set()
    assert await old is True
    # `old` was asked before the invalidation: its "yes" must not be stored.
    assert cache.cached(key) is False
    assert not cache._inflight and not cache._pending and not cache._stamp


async def test_answer_asked_before_invalidation_is_not_stored() -> None:
    gate = Stepped(True)
    cache = AccessCache(gate, gate, clock=Clock())  # type: ignore[arg-type]
    key = (uuid4(), DEMO, INSTALLATION_ID)
    old = asyncio.ensure_future(cache.check(key, TOKEN))
    await _settle()
    cache.invalidate(DEMO)
    gate.events[0].set()
    assert await old is True  # the caller still gets GitHub's answer
    assert cache.cached(key) is None


async def test_older_normal_check_never_overwrites_fresh() -> None:
    gate = Stepped(True, False)
    cache = AccessCache(gate, gate, clock=Clock())  # type: ignore[arg-type]
    key = (uuid4(), DEMO, INSTALLATION_ID)
    normal = asyncio.ensure_future(cache.check(key, TOKEN))
    await _settle()
    fresh = asyncio.ensure_future(cache.check(key, TOKEN, fresh=True))  # SSE re-check
    await _settle()
    gate.events[1].set()
    assert await fresh is False
    assert cache.cached(key) is False
    gate.events[0].set()
    assert await normal is True
    assert cache.cached(key) is False
    assert not cache._pending and not cache._stamp


async def test_unrelated_invalidation_keeps_inflight_store() -> None:
    gate = Stepped(True)
    cache = AccessCache(gate, gate, clock=Clock())  # type: ignore[arg-type]
    key = (uuid4(), DEMO, INSTALLATION_ID)
    check = asyncio.ensure_future(cache.check(key, TOKEN))
    await _settle()
    cache.invalidate_user(uuid4())
    cache.invalidate(PUBLIC)
    gate.events[0].set()
    assert await check is True
    assert cache.cached(key) is True


async def test_entries_expire_and_are_bounded() -> None:
    clock = Clock()
    cache, gate = _gate_cache(clock, max_entries=3)
    gate.release.set()
    keys = [(uuid4(), DEMO, INSTALLATION_ID) for _ in range(5)]
    for key in keys:
        await cache.check(key, TOKEN)
    assert len(cache) == 3
    assert cache.cached(keys[0]) is None  # oldest dropped
    assert cache.cached(keys[4]) is True
    await cache.check(keys[2], TOKEN, fresh=True)  # refreshed: now the newest
    await cache.check(keys[0], TOKEN)
    assert cache.cached(keys[2]) is True
    assert cache.cached(keys[3]) is None
    clock.advance(300)
    assert cache.cached(keys[2]) is None  # expired, and dropped on read
    assert len(cache) == 2


async def test_fresh_does_not_join_inflight_check() -> None:
    cache, gate = _gate_cache(Clock())
    key = (uuid4(), DEMO, INSTALLATION_ID)
    normal = asyncio.ensure_future(cache.check(key, TOKEN))
    await asyncio.sleep(0)
    gate.answer = False
    fresh = asyncio.ensure_future(cache.check(key, TOKEN, fresh=True))
    await asyncio.sleep(0)
    gate.release.set()
    assert await normal is False and await fresh is False
    assert gate.calls == 2


def test_actions_are_the_spec_set() -> None:
    assert {a.value for a in Action} == {
        "view",
        "read_files",
        "subscribe",
        "steer",
        "allow_blocked",
        "toggle_share",
        "delete",
    }
    assert frozenset(WRITES) == authz.OWNER_ONLY

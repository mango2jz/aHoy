"""T35: token crypto, login sessions (cookie), refresh under a per-user lock, logout, /api/me.

Sign-in is driven through `sessions.start` from a test-only route that does what T20's
callback will do with a mock-GitHub user: exchange a code, read the user, upsert, start.
"""

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from ahoy.app import create_app
from ahoy.auth import crypto, sessions
from ahoy.auth.sessions import DB, AuthState, hash_cookie
from ahoy.config import ConfigError, Settings
from ahoy.github.http import GitHubAuthError, GitHubHTTPError
from ahoy.github.mock import MockGitHub
from ahoy.github.user_client import GitHubUserInfo, UserTokens
from ahoy.store import auth as auth_store
from ahoy.store.db import make_engine, make_sessionmaker
from conftest import SameSession

NEAR_EXPIRY = timedelta(hours=7, minutes=56)  # mock tokens live 8 h; refresh margin is 5 min


# --- crypto (no database) ------------------------------------------------------------


def test_encrypt_round_trip_with_fresh_nonce() -> None:
    key, uid = crypto.generate_key(), crypto.token_aad(uuid4(), "access")
    ct1, n1 = crypto.encrypt(key, "ghu_secret", uid)
    ct2, n2 = crypto.encrypt(key, "ghu_secret", uid)
    assert len(n1) == len(n2) == crypto.NONCE_BYTES
    assert n1 != n2 and ct1 != ct2
    assert b"ghu_secret" not in ct1
    assert crypto.decrypt(key, ct1, n1, uid).get_secret_value() == "ghu_secret"


def test_decrypt_fails_with_wrong_aad_key_or_tamper() -> None:
    user = uuid4()
    key, uid = crypto.generate_key(), crypto.token_aad(user, "access")
    ct, nonce = crypto.encrypt(key, "ghu_secret", uid)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, ct, nonce, crypto.token_aad(uuid4(), "access"))
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, ct, nonce, crypto.token_aad(user, "refresh"))
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(crypto.generate_key(), ct, nonce, uid)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, ct[:-1] + bytes([ct[-1] ^ 1]), nonce, uid)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not base64 !!",
        base64.urlsafe_b64encode(b"x" * 16).decode(),
        base64.urlsafe_b64encode(b"x" * 33).decode(),
    ],
)
def test_load_key_rejects_bad_keys(value: str | None) -> None:
    with pytest.raises(ConfigError, match="TOKEN_ENCRYPTION_KEY") as info:
        crypto.load_key(None if value is None else SecretStr(value))
    if value:
        assert value not in str(info.value)


def test_load_key_accepts_unpadded_base64url() -> None:
    raw = bytes(range(32))
    unpadded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert crypto.load_key(SecretStr(unpadded)) == raw


def test_derive_subkey() -> None:
    key = crypto.generate_key()
    a = crypto.derive_subkey(key, b"ahoy-preauth")
    assert len(a) == 32
    assert a == crypto.derive_subkey(key, b"ahoy-preauth")
    assert a != crypto.derive_subkey(key, b"other")
    assert a != crypto.load_key(key)
    assert a != crypto.derive_subkey(crypto.generate_key(), b"ahoy-preauth")
    with pytest.raises(ValueError):
        crypto.derive_subkey(key, b"")


# Default APP_ORIGIN; state-changing requests need it plus X-Ahoy-CSRF (security.py).
CSRF = {"Origin": "http://127.0.0.1:5173", "X-Ahoy-CSRF": "1"}


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"ahoy_env": "test", "database_url": None}
    return Settings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


def test_app_startup_rejects_malformed_key() -> None:
    bad = "canary-not-a-key-1234"
    with pytest.raises(ConfigError, match="TOKEN_ENCRYPTION_KEY") as info:
        create_app(_settings(token_encryption_key=bad), "mock", "mock")
    assert bad not in str(info.value)


# --- app fixtures --------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, delta: timedelta) -> None:
        self.t += delta


class Harness:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.auth: AuthState = app.state.auth
        self.github: MockGitHub = app.state.github_user
        self.clock = Clock()
        self.auth.clock = self.clock
        self.issued: list[UserTokens] = []
        self.users: list[UUID] = []

    def client(self, base_url: str = "http://testserver") -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=base_url)

    async def sign_in(self, client: httpx.AsyncClient, name: str = "alice", **params: Any) -> str:
        resp = await client.get(f"/test/signin/{name}", params=params)
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/"
        value = client.cookies.get(sessions.SESSION_COOKIE_DEV)
        assert value
        return value


def _make_app() -> Harness:
    app = create_app(_settings(token_encryption_key=crypto.generate_key()), "mock", "mock")
    h = Harness(app)

    @app.get("/test/signin/{name}")
    async def signin(name: str, request: Request, db: DB, gid: int | None = None) -> Response:
        gh = h.github
        tokens = await gh.exchange_code(gh.issue_code(name), "", "")
        info = await gh.get_user(tokens.access_token)
        if gid is not None:
            info = info.model_copy(update={"id": gid, "login": f"{name}-{gid}"})
        user = await auth_store.upsert_user(db, info, default_model="mock")
        h.issued.append(tokens)
        h.users.append(user.id)
        return await sessions.start(request, db, user, tokens)

    return h


@pytest.fixture
def harness(db: AsyncSession) -> Harness:
    h = _make_app()

    async def override() -> AsyncIterator[AsyncSession]:
        yield db

    h.app.dependency_overrides[sessions.get_db] = override
    h.auth.sessionmaker = SameSession(db).maker()
    return h


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[httpx.AsyncClient]:
    async with harness.client() as c:
        yield c


async def _count(db: AsyncSession, table: str) -> int:
    result = await db.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
    count = int(result.scalar_one())
    await db.commit()
    return count


# --- sign-in, cookie, storage --------------------------------------------------------


@pytest.mark.db
async def test_sign_in_sets_dev_cookie_and_me_works(
    harness: Harness, client: httpx.AsyncClient
) -> None:
    resp = await client.get("/test/signin/alice")
    assert resp.status_code == 303
    cookies = resp.headers.get_list("set-cookie")
    sid = next(c for c in cookies if c.startswith("ahoy_sid="))
    attrs = [a.strip().lower() for a in sid.split(";")]
    assert "httponly" in attrs and "path=/" in attrs and "samesite=lax" in attrs
    assert "secure" not in attrs
    assert not any(a.startswith("domain") for a in attrs)
    pre = next(c for c in cookies if c.startswith("ahoy_preauth="))
    assert "max-age=0" in pre.lower()

    me = await client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert set(body) == {"id", "login", "avatar_url", "private_repo_notice_ack"}
    assert body["private_repo_notice_ack"] is False
    assert body["login"] == "alice"
    assert body["id"] == str(harness.users[0])


@pytest.mark.db
async def test_prod_cookie_is_host_prefixed_and_secure(harness: Harness) -> None:
    harness.auth.settings = _settings(ahoy_env="prod", app_origin="https://x.example")
    async with harness.client("https://testserver") as c:
        resp = await c.get("/test/signin/alice")
        cookies = resp.headers.get_list("set-cookie")
        sid = next(x for x in cookies if x.startswith("__Host-ahoy_sid="))
        attrs = [a.strip().lower() for a in sid.split(";")]
        assert {"secure", "httponly", "path=/", "samesite=lax"} <= set(attrs)
        assert not any(a.startswith("domain") for a in attrs)
        pre = next(x for x in cookies if x.startswith("__Host-ahoy_preauth="))
        assert "secure" in pre.lower() and "max-age=0" in pre.lower()
        assert not any(x.startswith("ahoy_sid=") for x in cookies)
        assert (await c.get("/api/me")).status_code == 200


@pytest.mark.db
async def test_only_cookie_hash_is_stored(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    value = await harness.sign_in(client)
    assert len(base64.urlsafe_b64decode(value + "==")) == sessions.COOKIE_VALUE_BYTES
    rows = await db.execute(text("SELECT id_hash, row_to_json(a)::text FROM auth_sessions a"))
    rows_all = rows.all()
    await db.commit()
    assert len(rows_all) == 1
    id_hash, dump = rows_all[0]
    assert bytes(id_hash) == hash_cookie(value)
    assert value not in dump
    assert value.encode() not in bytes(id_hash)


@pytest.mark.db
async def test_no_plaintext_token_in_db(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await harness.sign_in(client)
    tokens = harness.issued[0]
    secrets_ = [tokens.access_token.get_secret_value(), tokens.refresh_token.get_secret_value()]  # type: ignore[union-attr]
    dumps: list[str] = []
    blobs: list[bytes] = []
    for table in ("users", "github_tokens", "auth_sessions"):
        for (dump,) in await db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")):  # noqa: S608
            dumps.append(dump)
    for row in await db.execute(
        text("SELECT access_token_enc, refresh_token_enc FROM github_tokens")
    ):
        blobs.extend(bytes(b) for b in row)
    await db.commit()
    assert blobs
    for secret in secrets_:
        assert all(secret not in d for d in dumps)
        assert all(secret.encode() not in b for b in blobs)
    stored = await auth_store.get_tokens(db, harness.auth.key, harness.users[0])
    assert stored is not None
    assert stored.access_token.get_secret_value() == secrets_[0]
    assert stored.refresh_token.get_secret_value() == secrets_[1]
    # The rows are bound to the user: another user id (AAD) cannot decrypt them.
    row = (await db.execute(text("SELECT * FROM github_tokens"))).one()
    await db.commit()
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(
            harness.auth.key,
            row.access_token_enc,
            row.access_token_nonce,
            crypto.token_aad(uuid4(), "access"),
        )


@pytest.mark.db
async def test_swapped_token_columns_do_not_decrypt(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """The field name is in the AAD: access and refresh ciphertexts cannot be swapped."""
    await harness.sign_in(client)
    uid = harness.users[0]
    await db.execute(
        text(
            "UPDATE github_tokens SET"
            " access_token_enc = refresh_token_enc, access_token_nonce = refresh_token_nonce,"
            " refresh_token_enc = access_token_enc, refresh_token_nonce = access_token_nonce"
            " WHERE user_id = :u"
        ),
        {"u": uid},
    )
    await db.commit()
    with pytest.raises(crypto.CryptoError):
        await auth_store.get_tokens(db, harness.auth.key, uid)
    # The app treats undecryptable tokens as signed out.
    assert (await client.get("/api/me")).status_code == 401
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
async def test_new_cookie_value_every_sign_in(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    first = await harness.sign_in(client)
    second = await harness.sign_in(client)
    assert first != second
    assert await _count(db, "auth_sessions") == 1  # the replaced cookie's row is gone
    assert (await client.get("/api/me")).status_code == 200
    client.cookies.set(sessions.SESSION_COOKIE_DEV, first)
    assert (await client.get("/api/me")).status_code == 401
    # A second browser gets its own session; both stay valid.
    async with harness.client() as other:
        third = await harness.sign_in(other)
        assert third not in (first, second)
        assert (await other.get("/api/me")).status_code == 200
    assert await _count(db, "auth_sessions") == 2


@pytest.mark.db
async def test_upsert_user_by_github_id(db: AsyncSession) -> None:
    info = GitHubUserInfo(id=424242, login="old", avatar_url="https://a/1")
    first = await auth_store.upsert_user(db, info, default_model="m1")
    renamed = info.model_copy(update={"login": "new", "avatar_url": None})
    second = await auth_store.upsert_user(db, renamed, default_model="m2")
    assert second.id == first.id
    assert second.github_login == "new" and second.avatar_url == ""
    assert second.settings.model == "m1"  # settings are kept


# --- signed out, expiry, logout ------------------------------------------------------


@pytest.mark.db
async def test_me_401_signed_out(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/me")
    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers  # no cookie sent, nothing to clear
    client.cookies.set(sessions.SESSION_COOKIE_DEV, "garbage")
    resp = await client.get("/api/me")
    assert resp.status_code == 401
    _assert_cleared(resp)


def _assert_cleared(resp: httpx.Response) -> None:
    cleared = [c for c in resp.headers.get_list("set-cookie") if c.startswith("ahoy_sid=")]
    assert len(cleared) == 1
    attrs = [a.strip().lower() for a in cleared[0].split(";")]
    assert {"max-age=0", "path=/", "httponly", "samesite=lax"} <= set(attrs)


@pytest.mark.db
async def test_steady_state_request_only_reads(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await harness.sign_in(client)

    async def last_seen() -> datetime:
        value = (await db.execute(text("SELECT last_seen_at FROM auth_sessions"))).scalar_one()
        await db.commit()
        assert isinstance(value, datetime)
        return value

    statements: list[str] = []

    def record(_c: object, _cur: object, statement: str, *_rest: object) -> None:
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    sync_engine = (await db.connection()).sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        signed_in = await last_seen()
        harness.clock.advance(timedelta(seconds=30))
        statements.clear()
        assert (await client.get("/api/me")).status_code == 200
        writes = {"INSERT", "UPDATE", "DELETE"} & set(statements)
        assert statements and not writes  # a live session lookup only reads
        assert await last_seen() == signed_in  # within TOUCH_INTERVAL: no write
        harness.clock.advance(timedelta(seconds=30))
        statements.clear()
        assert (await client.get("/api/me")).status_code == 200
        assert "DELETE" not in statements and "UPDATE" in statements  # touch only
        assert await last_seen() == harness.clock()
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)


@pytest.mark.db
async def test_idle_timeout(harness: Harness, client: httpx.AsyncClient, db: AsyncSession) -> None:
    await harness.sign_in(client)
    harness.clock.advance(timedelta(days=6))
    assert (await client.get("/api/me")).status_code == 200  # touches last_seen_at
    harness.clock.advance(timedelta(days=6, hours=23))
    assert (await client.get("/api/me")).status_code == 200
    harness.clock.advance(timedelta(days=7))
    assert (await client.get("/api/me")).status_code == 401
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
async def test_absolute_lifetime(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await harness.sign_in(client)
    for _ in range(4):
        harness.clock.advance(timedelta(days=6))
        assert (await client.get("/api/me")).status_code == 200
    harness.clock.advance(timedelta(days=5, hours=23))  # 29 d 23 h
    assert (await client.get("/api/me")).status_code == 200
    harness.clock.advance(timedelta(hours=1))  # 30 d
    assert (await client.get("/api/me")).status_code == 401
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
async def test_logout(harness: Harness, client: httpx.AsyncClient, db: AsyncSession) -> None:
    value = await harness.sign_in(client)
    resp = await client.post("/auth/logout", headers=CSRF)
    assert resp.status_code == 204
    cleared = next(c for c in resp.headers.get_list("set-cookie") if c.startswith("ahoy_sid="))
    assert "max-age=0" in cleared.lower()
    assert await _count(db, "auth_sessions") == 0
    client.cookies.set(sessions.SESSION_COOKIE_DEV, value)
    assert (await client.get("/api/me")).status_code == 401
    # Logging out while signed out is harmless.
    client.cookies.clear()
    assert (await client.post("/auth/logout", headers=CSRF)).status_code == 204


@pytest.mark.db
async def test_delete_user_auth(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await harness.sign_in(client)
    await auth_store.delete_user_auth(db, harness.users[0])
    assert await _count(db, "auth_sessions") == 0
    assert await _count(db, "github_tokens") == 0
    assert (await client.get("/api/me")).status_code == 401


# --- refresh -------------------------------------------------------------------------


def _count_refresh(monkeypatch: pytest.MonkeyPatch, h: "Harness", delay: float = 0.0) -> list[int]:
    """Count refresh calls. New tokens expire 8 h after the test clock (GitHub's time is the
    app's time), not after the real clock the mock uses."""
    calls: list[int] = []
    gh = h.github
    original: Callable[[SecretStr], Awaitable[UserTokens]] = gh.refresh

    async def refresh(token: SecretStr) -> UserTokens:
        calls.append(1)
        await asyncio.sleep(delay)
        fresh = await original(token)
        return fresh.model_copy(update={"expires_at": h.clock() + timedelta(hours=8)})

    monkeypatch.setattr(gh, "refresh", refresh)
    return calls


@pytest.mark.db
async def test_refresh_near_expiry_rotates_both_tokens(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_refresh(monkeypatch, harness)
    await harness.sign_in(client)
    assert (await client.get("/api/me")).status_code == 200
    assert calls == []
    harness.clock.advance(NEAR_EXPIRY)
    assert (await client.get("/api/me")).status_code == 200
    assert len(calls) == 1
    stored = await auth_store.get_tokens(db, harness.auth.key, harness.users[0])
    assert stored is not None
    old = harness.issued[0]
    assert stored.access_token.get_secret_value() != old.access_token.get_secret_value()
    assert stored.refresh_token.get_secret_value() != old.refresh_token.get_secret_value()  # type: ignore[union-attr]


@pytest.mark.db
async def test_refresh_failure_ends_session(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await harness.sign_in(client)
    async with harness.client() as other:
        await harness.sign_in(other)
    assert await _count(db, "auth_sessions") == 2
    # Revoking the grant kills the refresh token too, so the refresh is refused.
    await harness.github.revoke_grant(harness.issued[0].access_token)
    harness.clock.advance(NEAR_EXPIRY)
    resp = await client.get("/api/me")
    assert resp.status_code == 401
    _assert_cleared(resp)
    assert await _count(db, "auth_sessions") == 0
    assert await _count(db, "github_tokens") == 0
    assert (await client.get("/api/me")).status_code == 401


@pytest.mark.db
async def test_refresh_outage_keeps_session(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.sign_in(client)

    async def down(token: SecretStr) -> UserTokens:
        raise GitHubHTTPError("GitHub 502", status=502)

    monkeypatch.setattr(harness.github, "refresh", down)
    harness.clock.advance(NEAR_EXPIRY)
    assert (await client.get("/api/me")).status_code == 503
    assert await _count(db, "auth_sessions") == 1


@pytest.mark.db
async def test_refresh_error_does_not_leak_tokens(
    harness: Harness, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.sign_in(client)
    secret = harness.issued[0].refresh_token.get_secret_value()  # type: ignore[union-attr]

    async def refused(token: SecretStr) -> UserTokens:
        raise GitHubAuthError(f"refused {token.get_secret_value()}")

    monkeypatch.setattr(harness.github, "refresh", refused)
    harness.clock.advance(NEAR_EXPIRY)
    resp = await client.get("/api/me")
    assert resp.status_code == 401
    assert secret not in resp.text


@pytest.mark.db
async def test_expired_refresh_token_ends_session(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_refresh(monkeypatch, harness)
    await harness.sign_in(client)
    harness.clock.advance(NEAR_EXPIRY)
    await db.execute(
        text("UPDATE github_tokens SET refresh_expires_at = :t"),
        {"t": harness.clock() - timedelta(seconds=1)},
    )
    await db.commit()
    resp = await client.get("/api/me")
    assert resp.status_code == 401
    _assert_cleared(resp)
    assert calls == []  # GitHub is not asked with a dead refresh token
    assert await _count(db, "auth_sessions") == 0
    assert await _count(db, "github_tokens") == 0


@pytest.mark.db
async def test_refresh_without_refresh_token_ends_session(
    harness: Harness, client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.sign_in(client)

    async def no_refresh_token(token: SecretStr) -> UserTokens:
        return UserTokens(
            access_token=SecretStr("ghu_" + "n" * 36),
            expires_at=harness.clock() + timedelta(hours=8),
        )

    monkeypatch.setattr(harness.github, "refresh", no_refresh_token)
    harness.clock.advance(NEAR_EXPIRY)
    resp = await client.get("/api/me")
    assert resp.status_code == 401
    _assert_cleared(resp)
    assert await _count(db, "auth_sessions") == 0
    assert await _count(db, "github_tokens") == 0


@pytest.fixture
async def sm(test_database: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(test_database, poolclass=NullPool)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.mark.db
async def test_concurrent_near_expiry_refreshes_once(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests on separate connections (committed data) notice the expiry together."""
    h = _make_app()
    h.auth.sessionmaker = sm
    calls = _count_refresh(monkeypatch, h, delay=0.1)
    gid = uuid4().int % 10**12
    try:
        async with h.client() as c:
            await h.sign_in(c, gid=gid)
            h.clock.advance(NEAR_EXPIRY)
            first, second = await asyncio.gather(c.get("/api/me"), c.get("/api/me"))
            assert (first.status_code, second.status_code) == (200, 200)
            assert len(calls) == 1
            assert (await c.get("/api/me")).status_code == 200
            assert len(calls) == 1
    finally:
        async with sm() as db:
            await db.execute(text("DELETE FROM users WHERE github_id = :g"), {"g": gid})
            await db.commit()


@pytest.mark.db
async def test_cancelled_request_still_saves_refreshed_tokens(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client goes away mid-refresh: GitHub already rotated, so the new pair is saved."""
    h = _make_app()
    h.auth.sessionmaker = sm
    calls = _count_refresh(monkeypatch, h, delay=0.2)
    gid = uuid4().int % 10**12
    try:
        async with h.client() as c:
            await h.sign_in(c, gid=gid)
            h.clock.advance(NEAR_EXPIRY)
            request = asyncio.ensure_future(c.get("/api/me"))
            while not calls:
                await asyncio.sleep(0.01)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert h.auth.tasks  # the refresh is still running
            await asyncio.gather(*h.auth.tasks)
            async with sm() as db:
                stored = await auth_store.get_tokens(db, h.auth.key, h.users[0])
            assert stored is not None
            assert stored.access_expires_at > h.clock() + timedelta(hours=7)
            assert (await c.get("/api/me")).status_code == 200
            assert len(calls) == 1
    finally:
        async with sm() as db:
            await db.execute(text("DELETE FROM users WHERE github_id = :g"), {"g": gid})
            await db.commit()

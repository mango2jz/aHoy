"""T20: GitHub sign-in (login -> pre-auth cookie -> callback) and the dev/test mock login.

The flow runs against mock GitHub: the test plays GitHub's authorize page by issuing a code
bound to the challenge from the login redirect. One test drives the real HTTP client with
respx to pin the token-endpoint error mapping. Nothing calls real GitHub.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy import app as app_module
from ahoy.app import create_app
from ahoy.auth import crypto, github_oauth, sessions
from ahoy.auth.sessions import AuthState
from ahoy.config import ConfigError, Settings
from ahoy.github.http import GitHubHTTPError
from ahoy.github.mock import MockGitHub
from ahoy.github.user_client import GitHubUserInfo, UserTokens
from ahoy.store import auth as auth_store
from conftest import SameSession

ORIGIN = "http://127.0.0.1:5173"
PREAUTH = sessions.PREAUTH_COOKIE_DEV
SID = sessions.SESSION_COOKIE_DEV


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "ahoy_env": "test",
        "database_url": None,
        "app_origin": ORIGIN,
        "token_encryption_key": crypto.generate_key(),
    }
    return Settings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


def _real_settings(**kw: Any) -> Settings:
    return _settings(
        github_app_private_key="-----BEGIN KEY-----x",
        github_client_secret="client-secret-value",
        github_webhook_secret="webhook-secret",
        github_client_id="Iv1.testclient",
        github_app_id="12345",
        **kw,
    )


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t


def _bind_db(app: FastAPI, db: AsyncSession) -> AuthState:
    async def override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[sessions.get_db] = override
    auth: AuthState = app.state.auth
    auth.sessionmaker = SameSession(db).maker()
    auth.clock = Clock()
    return auth


class Flow:
    def __init__(self, app: FastAPI, auth: AuthState) -> None:
        self.app = app
        self.auth = auth
        self.github: MockGitHub = app.state.github_user
        self.clock = cast(Clock, auth.clock)

    def client(self, base_url: str = "http://testserver") -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=base_url)

    async def login(self, client: httpx.AsyncClient) -> dict[str, str]:
        """GET /auth/github/login; returns the authorize query params."""
        resp = await client.get("/auth/github/login")
        assert resp.status_code == 302
        url = urlsplit(resp.headers["location"])
        assert f"{url.scheme}://{url.netloc}{url.path}" == github_oauth.AUTHORIZE_URL
        return {k: v[0] for k, v in parse_qs(url.query).items()}

    async def authorize(self, client: httpx.AsyncClient, user: str = "alice") -> tuple[str, str]:
        """Login, then what GitHub's authorize page does: (code, state)."""
        params = await self.login(client)
        return self.github.issue_code(user, params["code_challenge"]), params["state"]


@pytest.fixture
def flow(db: AsyncSession) -> Flow:
    app = create_app(_settings(), "mock", "mock")
    return Flow(app, _bind_db(app, db))


@pytest.fixture
async def client(flow: Flow) -> AsyncIterator[httpx.AsyncClient]:
    async with flow.client() as c:
        yield c


def _set_cookie(resp: httpx.Response, name: str) -> list[str]:
    header = next(c for c in resp.headers.get_list("set-cookie") if c.startswith(f"{name}="))
    return [a.strip().lower() for a in header.split(";")]


def _payload(value: str) -> dict[str, Any]:
    payload = value.split(".")[0]
    data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return data


async def _count(db: AsyncSession, table: str) -> int:
    count = int((await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608
    await db.commit()
    return count


def _assert_failed(resp: httpx.Response, status: int = 400) -> None:
    assert resp.status_code == status
    assert resp.json() == {
        "detail": "sign-in failed, start again"
        if status == 400
        else "sign-in unavailable, try again"
    }
    assert "max-age=0" in _set_cookie(resp, PREAUTH)
    assert not any(c.startswith(f"{SID}=") for c in resp.headers.get_list("set-cookie"))


# --- login ---------------------------------------------------------------------------


@pytest.mark.db
async def test_login_sets_preauth_cookie_and_redirects_with_s256(
    flow: Flow, client: httpx.AsyncClient
) -> None:
    resp = await client.get("/auth/github/login")
    assert resp.status_code == 302
    attrs = _set_cookie(resp, PREAUTH)
    assert {"httponly", "samesite=lax", "path=/", "max-age=600"} <= set(attrs)
    assert "secure" not in attrs and not any(a.startswith("domain") for a in attrs)

    query = parse_qs(urlsplit(resp.headers["location"]).query, keep_blank_values=True)
    params = {k: v[0] for k, v in query.items()}
    assert set(params) == {
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
    }
    assert params["redirect_uri"] == ORIGIN + "/auth/github/callback"
    assert params["code_challenge_method"] == "S256"

    data = _payload(client.cookies[PREAUTH])
    assert data["state"] == params["state"] and len(data["state"]) >= 43
    assert 43 <= len(data["verifier"]) <= 128
    digest = hashlib.sha256(data["verifier"].encode()).digest()
    assert params["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert data["verifier"] not in resp.headers["location"]
    assert data["exp"] == int(flow.clock().timestamp()) + 600

    again = await flow.login(client)
    assert again["state"] != params["state"]  # fresh state every time


async def test_login_redirect_uses_client_id() -> None:
    app = create_app(_real_settings(), "mock", "real")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/auth/github/login")
    params = parse_qs(urlsplit(resp.headers["location"]).query)
    assert params["client_id"] == ["Iv1.testclient"]


@pytest.mark.db
async def test_prod_preauth_cookie_is_host_prefixed_and_secure(flow: Flow) -> None:
    flow.auth.settings = _settings(ahoy_env="prod", app_origin="https://x.example")
    async with flow.client("https://testserver") as c:
        resp = await c.get("/auth/github/login")
    attrs = _set_cookie(resp, sessions.PREAUTH_COOKIE_PROD)
    assert {"secure", "httponly", "samesite=lax", "path=/", "max-age=600"} <= set(attrs)
    assert not any(c.startswith(f"{PREAUTH}=") for c in resp.headers.get_list("set-cookie"))
    redirect = parse_qs(urlsplit(resp.headers["location"]).query)["redirect_uri"]
    assert redirect == ["https://x.example/auth/github/callback"]


# --- callback ------------------------------------------------------------------------


@pytest.mark.db
async def test_callback_signs_in_keyed_by_github_id(
    flow: Flow, client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, state = await flow.authorize(client)
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert client.cookies.get(SID)
    assert "max-age=0" in _set_cookie(resp, PREAUTH)
    assert PREAUTH not in client.cookies

    me = await client.get("/api/me")
    assert me.status_code == 200 and me.json()["login"] == "alice"
    row = (await db.execute(text("SELECT id, github_id FROM users"))).one()
    await db.commit()
    assert row.github_id == 1001 and str(row.id) == me.json()["id"]
    stored = await auth_store.get_tokens(db, flow.auth.key, row.id)
    assert stored is not None

    # A renamed GitHub login stays the same aHoy user (keyed by the numeric id).
    real_get_user = flow.github.get_user

    async def renamed(token: Any) -> GitHubUserInfo:
        info = await real_get_user(token)
        return info.model_copy(update={"login": "alice-renamed"})

    monkeypatch.setattr(flow.github, "get_user", renamed)
    code, state = await flow.authorize(client)
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    assert resp.status_code == 303
    me2 = await client.get("/api/me")
    assert me2.json() == {**me.json(), "login": "alice-renamed"}
    assert await _count(db, "users") == 1


@pytest.mark.db
async def test_wrong_state_400(flow: Flow, client: httpx.AsyncClient, db: AsyncSession) -> None:
    code, state = await flow.authorize(client)
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state + "x"})
    _assert_failed(resp)
    resp = await client.get("/auth/github/callback", params={"code": code})
    _assert_failed(resp)
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
async def test_missing_preauth_cookie_400(
    flow: Flow, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    code, state = await flow.authorize(client)
    client.cookies.delete(PREAUTH)
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp)
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
@pytest.mark.parametrize("part", ["payload", "signature"])
async def test_tampered_preauth_cookie_400(
    flow: Flow, client: httpx.AsyncClient, db: AsyncSession, part: str
) -> None:
    code, state = await flow.authorize(client)
    payload, sig = client.cookies[PREAUTH].split(".")
    if part == "payload":
        data = _payload(client.cookies[PREAUTH]) | {"exp": 2**40}
        raw = json.dumps(data, separators=(",", ":")).encode()
        payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    else:
        sig = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    client.cookies.set(PREAUTH, f"{payload}.{sig}")
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp)
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
async def test_expired_preauth_cookie_400(
    flow: Flow, client: httpx.AsyncClient, db: AsyncSession
) -> None:
    code, state = await flow.authorize(client)
    flow.clock.t += timedelta(seconds=601)  # the browser would have dropped it; the server too
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp)
    assert await _count(db, "auth_sessions") == 0


@pytest.mark.db
async def test_preauth_cookie_from_another_key_400(flow: Flow, client: httpx.AsyncClient) -> None:
    code, state = await flow.authorize(client)
    data = _payload(client.cookies[PREAUTH])
    other = AuthState(
        settings=flow.auth.settings,
        key=crypto.generate_key(),
        github=flow.github,
        default_model="mock",
        clock=flow.clock,
    )
    forged = github_oauth.encode_preauth(other, data["state"], data["verifier"], data["exp"])
    client.cookies.set(PREAUTH, forged)
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp)


@pytest.mark.db
async def test_replayed_code_fails(flow: Flow, client: httpx.AsyncClient, db: AsyncSession) -> None:
    code, state = await flow.authorize(client)
    preauth = client.cookies[PREAUTH]
    url = "/auth/github/callback"
    assert (await client.get(url, params={"code": code, "state": state})).status_code == 303
    assert await _count(db, "auth_sessions") == 1
    # An attacker holding the whole first request (cookie included) replays it.
    async with flow.client() as other:
        other.cookies.set(PREAUTH, preauth)
        resp = await other.get(url, params={"code": code, "state": state})
        _assert_failed(resp)
        assert SID not in other.cookies
    assert await _count(db, "auth_sessions") == 1


@pytest.mark.db
async def test_install_redirect_bounces_without_exchanging(
    flow: Flow, client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchanged: list[str] = []

    async def spy(code: str, verifier: str, redirect_uri: str) -> UserTokens:
        exchanged.append(code)
        raise AssertionError("must not exchange")

    monkeypatch.setattr(flow.github, "exchange_code", spy)
    code = flow.github.issue_code("alice")
    resp = await client.get(
        "/auth/github/callback",
        params={"code": code, "installation_id": "5001", "setup_action": "install"},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/github/login"
    assert exchanged == []
    assert await _count(db, "auth_sessions") == 0
    # With a state of ours it is a normal callback again (here: no cookie -> 400).
    resp = await client.get(
        "/auth/github/callback",
        params={"code": code, "state": "x", "setup_action": "install"},
    )
    _assert_failed(resp)
    assert exchanged == []


@pytest.mark.db
async def test_github_outage_during_exchange_503(
    flow: Flow, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(code: str, verifier: str, redirect_uri: str) -> UserTokens:
        raise GitHubHTTPError("GitHub POST /login/oauth/access_token returned 502", status=502)

    code, state = await flow.authorize(client)
    monkeypatch.setattr(flow.github, "exchange_code", down)
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp, 503)


@pytest.mark.db
async def test_real_client_error_mapping(db: AsyncSession) -> None:
    """The real HTTP client: an OAuth error body is a 400, a 5xx is a 503."""
    app = create_app(_real_settings(), "mock", "real")
    _bind_db(app, db)
    token_url = "https://github.com/login/oauth/access_token"
    with respx.mock(assert_all_called=False) as router:
        route = router.post(token_url)
        router.route(host="testserver").pass_through()
        await _drive_errors(app, route)


async def _drive_errors(app: FastAPI, route: respx.Route) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        for reply, status in (
            (httpx.Response(200, json={"error": "bad_verification_code"}), 400),
            (httpx.Response(502, json={"message": "bad gateway"}), 503),
        ):
            route.mock(return_value=reply)
            resp = await c.get("/auth/github/login")
            state = parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]
            verifier = _payload(c.cookies[PREAUTH])["verifier"]
            resp = await c.get("/auth/github/callback", params={"code": "abc123", "state": state})
            _assert_failed(resp, status)
            form = parse_qs(route.calls.last.request.content.decode())
            assert form["code"] == ["abc123"] and form["code_verifier"] == [verifier]
            assert form["redirect_uri"] == [ORIGIN + "/auth/github/callback"]
            assert form["client_id"] == ["Iv1.testclient"]


# --- mock login ----------------------------------------------------------------------


@pytest.mark.db
async def test_mock_login_signs_in(flow: Flow, client: httpx.AsyncClient, db: AsyncSession) -> None:
    resp = await client.get("/auth/mock/login", params={"user": "bob"})
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    me = await client.get("/api/me")
    assert me.status_code == 200 and me.json()["login"] == "bob"
    row = (await db.execute(text("SELECT id, github_id FROM users"))).one()
    await db.commit()
    assert row.github_id == 1002
    stored = await auth_store.get_tokens(db, flow.auth.key, row.id)
    assert stored is not None and stored.access_token.get_secret_value().startswith("ghu_")


@pytest.mark.db
@pytest.mark.parametrize("params", [{"user": "eve"}, {}])
async def test_mock_login_unknown_user_400(
    client: httpx.AsyncClient, db: AsyncSession, params: dict[str, str]
) -> None:
    resp = await client.get("/auth/mock/login", params=params)
    assert resp.status_code == 400
    assert await _count(db, "auth_sessions") == 0


def _route_paths(routes: Any) -> set[str]:
    """Every route path, looking inside included routers (FastAPI wraps them)."""
    paths: set[str] = set()
    for route in routes:
        sub = getattr(route, "original_router", None)
        if sub is not None:
            paths |= _route_paths(sub.routes)
        elif isinstance(getattr(route, "path", None), str):
            paths.add(route.path)
    return paths


def _has_route(app: FastAPI, path: str) -> bool:
    found = path in _route_paths(app.routes)
    assert found == (path in app.openapi()["paths"])
    return found


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param(_real_settings, id="dev-real"),
        pytest.param(
            lambda: _real_settings(ahoy_env="prod", app_origin="https://x.example"), id="prod-real"
        ),
    ],
)
async def test_mock_login_absent_without_mock_github(settings: Any) -> None:
    app = create_app(settings(), "mock", "real")
    assert not _has_route(app, "/auth/mock/login")
    assert _has_route(app, "/auth/github/login")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        resp = await c.get("/auth/mock/login", params={"user": "alice"})
    assert resp.status_code == 404


def test_mock_login_present_with_mock_github() -> None:
    assert _has_route(create_app(_settings(), "mock", "mock"), "/auth/mock/login")


def test_mock_github_refused_in_prod() -> None:
    with pytest.raises(ConfigError, match="AHOY_ENV"):
        create_app(_settings(ahoy_env="prod", app_origin="https://x.example"), "mock", "mock")


@pytest.mark.parametrize(
    ("field", "name"),
    [("github_client_id", "GITHUB_CLIENT_ID"), ("github_app_id", "GITHUB_APP_ID")],
)
def test_real_github_requires_client_and_app_id(field: str, name: str) -> None:
    for value in (None, "  "):
        settings = _real_settings()
        setattr(settings, field, value)
        with pytest.raises(ConfigError, match=name):
            create_app(settings, "mock", "real")


# --- review follow-ups ---------------------------------------------------------------


def _signed(auth: AuthState, data: dict[str, Any]) -> str:
    """A pre-auth cookie value with a valid MAC over arbitrary JSON."""
    raw = json.dumps(data, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    key = crypto.derive_subkey(auth.key, b"ahoy-preauth")
    mac = hmac.new(key, payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


@pytest.mark.db
@pytest.mark.parametrize("exp", ["9999999999", 9999999999.0, True, None, [1]])
async def test_preauth_exp_must_be_int(flow: Flow, client: httpx.AsyncClient, exp: Any) -> None:
    code, state = await flow.authorize(client)
    data = _payload(client.cookies[PREAUTH]) | {"exp": exp}
    client.cookies.set(PREAUTH, _signed(flow.auth, data))
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp)


@pytest.mark.db
async def test_resigned_valid_payload_is_accepted(flow: Flow, client: httpx.AsyncClient) -> None:
    """Control for the test above: `_signed` builds cookies the server accepts."""
    code, state = await flow.authorize(client)
    client.cookies.set(PREAUTH, _signed(flow.auth, _payload(client.cookies[PREAUTH])))
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    assert resp.status_code == 303


@pytest.mark.parametrize("junk", ["AAAA=", "AA AA", "AA+/", "AA.A", "AB"])
def test_unb64_is_strict_and_canonical(junk: str) -> None:
    with pytest.raises(ValueError):
        github_oauth._unb64(junk)
    assert github_oauth._unb64("AA") == b"\x00"


@pytest.mark.db
async def test_noncanonical_signature_rejected(flow: Flow, client: httpx.AsyncClient) -> None:
    code, state = await flow.authorize(client)
    payload, sig = client.cookies[PREAUTH].split(".")
    # 32-byte MAC -> 43 chars; the last carries 2 unused bits. Setting them keeps the bytes.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(sig[-1])
    assert last & 3 == 0
    client.cookies.set(PREAUTH, f"{payload}.{sig[:-1]}{alphabet[last | 1]}")
    resp = await client.get("/auth/github/callback", params={"code": code, "state": state})
    _assert_failed(resp)


async def test_install_bounce_needs_no_database() -> None:
    """The bounce happens before any DB access (here there is no database at all)."""
    app = create_app(_settings(), "mock", "mock")
    assert app.state.auth.sessionmaker is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get(
            "/auth/github/callback", params={"code": "abc", "setup_action": "install"}
        )
        assert resp.status_code == 302 and resp.headers["location"] == "/auth/github/login"
        # A request we reject is also answered without a database.
        _assert_failed(await c.get("/auth/github/callback", params={"code": "a", "state": "b"}))


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.db
async def test_request_log_shows_signed_in_user(flow: Flow, client: httpx.AsyncClient) -> None:
    assert (await client.get("/auth/mock/login", params={"user": "alice"})).status_code == 303
    handler = _Records()
    log = logging.getLogger("ahoy.request")
    log.addHandler(handler)
    old = log.level
    log.setLevel(logging.INFO)
    try:
        me = await client.get("/api/me")
        assert me.status_code == 200
        client.cookies.clear()
        assert (await client.get("/api/me")).status_code == 401
    finally:
        log.removeHandler(handler)
        log.setLevel(old)
    users = [r.getMessage().split("user=", 1)[1].split()[0] for r in handler.records]
    assert users == [me.json()["id"], "-"]


class _FakeEngine:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def dispose(self) -> None:
        self.events.append("dispose")


async def _lifespan_with(
    monkeypatch: pytest.MonkeyPatch, *jobs: Any
) -> tuple[list[str], float, AuthState]:
    events: list[str] = []
    monkeypatch.setattr(app_module, "make_engine", lambda *a, **k: _FakeEngine(events))
    monkeypatch.setattr(app_module, "make_sessionmaker", lambda engine: None)
    app = create_app(_settings(database_url="postgresql+asyncpg://u:p@h/db"), "mock", "mock")
    auth: AuthState = app.state.auth
    async with app.router.lifespan_context(app):
        for job in jobs:
            task = asyncio.ensure_future(job(events))
            auth.tasks.add(task)
            task.add_done_callback(auth.tasks.discard)
        start = time.monotonic()
    return events, time.monotonic() - start, auth


async def test_shutdown_drains_refreshes_before_engine_dispose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(events: list[str]) -> None:
        await asyncio.sleep(0.2)
        events.append("saved")

    events, _, auth = await _lifespan_with(monkeypatch, slow)
    assert events == ["saved", "dispose"]
    assert not auth.tasks


async def test_shutdown_drain_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "SHUTDOWN_DRAIN_S", 0.3)

    async def hang(events: list[str]) -> None:
        await asyncio.sleep(100)

    events, waited, auth = await _lifespan_with(monkeypatch, hang)
    assert events == ["dispose"]
    assert 0.25 <= waited < 2
    for task in auth.tasks:
        task.cancel()

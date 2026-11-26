"""CSRF, security headers and trusted proxies (security.py). No database needed."""

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from ahoy.app import create_app
from ahoy.config import Settings
from ahoy.security import CSP, HSTS

ORIGIN = "http://127.0.0.1:5173"
CSRF = {"Origin": ORIGIN, "X-Ahoy-CSRF": "1"}


@pytest.fixture(autouse=True)
def _restore_raise_exceptions() -> Iterator[None]:
    """A prod app sets logging.raiseExceptions = False process-wide; undo it after each test."""
    saved = logging.raiseExceptions
    yield
    logging.raiseExceptions = saved


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"ahoy_env": "test", "database_url": None, "app_origin": ORIGIN}
    return Settings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


def _app(**kw: Any) -> FastAPI:
    app = create_app(_settings(**kw), "mock", "mock")

    @app.api_route("/api/echo", methods=["POST", "PUT", "PATCH", "DELETE", "GET"])
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/denied")
    async def denied() -> None:
        raise HTTPException(401, "not signed in")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for i in range(3):
                yield f"data: {i}\n\n".encode()

        return StreamingResponse(chunks(), media_type="text/event-stream")

    @app.get("/peer")
    async def peer(request: Request) -> dict[str, str]:
        assert request.client is not None
        return {"host": request.client.host, "scheme": request.url.scheme}

    return app


def _client(app: FastAPI, peer: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(peer, 5555))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with _client(_app()) as c:
        yield c


# --- CSRF ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_state_changing_needs_origin_and_header(
    client: httpx.AsyncClient, method: str
) -> None:
    ok = await client.request(method, "/api/echo", headers=CSRF)
    assert ok.status_code == 200, ok.text
    for headers in (
        {},  # nothing
        {"X-Ahoy-CSRF": "1"},  # missing Origin
        {"Origin": ORIGIN},  # same origin, no header
        {"Origin": "https://evil.example", "X-Ahoy-CSRF": "1"},  # cross origin
        {"Origin": ORIGIN + "/", "X-Ahoy-CSRF": "1"},  # not exactly equal
        {"Origin": "null", "X-Ahoy-CSRF": "1"},
        {"Origin": ORIGIN, "X-Ahoy-CSRF": "0"},
        {"Origin": ORIGIN, "X-Ahoy-CSRF": "true"},
    ):
        resp = await client.request(method, "/api/echo", headers=headers)
        assert resp.status_code == 403, headers
        assert resp.json() == {"detail": "csrf"}
        assert resp.headers["content-security-policy"] == CSP


async def test_csrf_runs_before_the_app(client: httpx.AsyncClient) -> None:
    # A route that does not exist still gets 403, not 404: the app never ran.
    resp = await client.post("/api/nope", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    # Logout is not exempt.
    assert (await client.post("/auth/logout")).status_code == 403


async def test_webhook_exempt(client: httpx.AsyncClient) -> None:
    # The real route runs (401: no webhook secret configured), not the CSRF check (403).
    resp = await client.post("/webhooks/github", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 401
    assert (await client.post("/webhooks/github")).status_code == 401
    # Exact path only.
    assert (await client.post("/webhooks/github/x")).status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_safe_methods_unaffected(client: httpx.AsyncClient, method: str) -> None:
    resp = await client.request(method, "/api/echo", headers={"Origin": "https://evil.example"})
    assert resp.status_code != 403


# --- security headers ---------------------------------------------------------------


def _assert_headers(resp: httpx.Response, *, hsts: bool) -> None:
    assert resp.headers["content-security-policy"] == CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert ("strict-transport-security" in resp.headers) is hsts
    if hsts:
        assert resp.headers["strict-transport-security"] == HSTS
    for name in ("content-security-policy", "x-content-type-options", "referrer-policy"):
        assert len(resp.headers.get_list(name)) == 1


async def test_headers_on_every_response(client: httpx.AsyncClient) -> None:
    ok = await client.get("/api/health")
    assert ok.status_code == 200
    _assert_headers(ok, hsts=False)
    missing = await client.get("/no/such/page")
    assert missing.status_code == 404
    _assert_headers(missing, hsts=False)
    unauth = await client.get("/denied")
    assert unauth.status_code == 401
    _assert_headers(unauth, hsts=False)
    crash = await client.get("/boom")
    assert crash.status_code == 500
    _assert_headers(crash, hsts=False)


async def test_headers_on_streaming_response(client: httpx.AsyncClient) -> None:
    async with client.stream("GET", "/stream") as resp:
        assert resp.status_code == 200
        _assert_headers(resp, hsts=False)
        body = b"".join([chunk async for chunk in resp.aiter_bytes()])
    assert body == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


async def test_existing_header_not_duplicated() -> None:
    app = _app()

    @app.get("/custom")
    async def custom() -> StreamingResponse:
        return StreamingResponse(
            iter([b"x"]), headers={"Content-Security-Policy": "default-src 'none'"}
        )

    async with _client(app) as c:
        resp = await c.get("/custom")
    assert resp.headers.get_list("content-security-policy") == ["default-src 'none'"]


async def test_hsts_only_in_prod() -> None:
    async with _client(_app(ahoy_env="dev")) as c:
        _assert_headers(await c.get("/api/health"), hsts=False)
    prod = _app()
    # --github mock refuses prod, so swap the settings in before the stack is built.
    prod.ahoy_settings = _settings(ahoy_env="prod", app_origin="https://ahoy.example")  # type: ignore[attr-defined]
    async with _client(prod) as c:
        _assert_headers(await c.get("/api/health"), hsts=True)
        _assert_headers(await c.get("/no/such/page"), hsts=True)
        _assert_headers(await c.post("/api/echo"), hsts=True)  # the CSRF 403 too


# --- trusted proxies ----------------------------------------------------------------

FORWARDED = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}


async def test_forwarded_ignored_without_trusted_proxies() -> None:
    async with _client(_app(), peer="10.0.0.2") as c:
        resp = await c.get("/peer", headers=FORWARDED)
    assert resp.json() == {"host": "10.0.0.2", "scheme": "http"}


async def test_forwarded_ignored_from_untrusted_peer() -> None:
    app = _app(trusted_proxy_ips="10.0.0.1, 10.0.0.3")
    async with _client(app, peer="10.0.0.2") as c:
        resp = await c.get("/peer", headers=FORWARDED)
    assert resp.json() == {"host": "10.0.0.2", "scheme": "http"}


async def test_forwarded_used_from_trusted_peer() -> None:
    app = _app(trusted_proxy_ips="10.0.0.1, 10.0.0.3")
    async with _client(app, peer="10.0.0.3") as c:
        resp = await c.get("/peer", headers=FORWARDED)
    assert resp.json() == {"host": "203.0.113.9", "scheme": "https"}


async def test_duplicate_csrf_headers_rejected(client: httpx.AsyncClient) -> None:
    for headers in (
        [("Origin", ORIGIN), ("Origin", ORIGIN), ("X-Ahoy-CSRF", "1")],
        [("Origin", ORIGIN), ("X-Ahoy-CSRF", "1"), ("X-Ahoy-CSRF", "1")],
    ):
        assert (await client.post("/api/echo", headers=headers)).status_code == 403


async def _raw(app: FastAPI, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if scope["type"] == "websocket":
            return {"type": "websocket.connect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _scope(kind: str, method: str, path: str) -> dict[str, Any]:
    return {
        "type": kind,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http" if kind == "http" else "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"origin", ORIGIN.encode()), (b"x-ahoy-csrf", b"1")],
        "client": ("127.0.0.1", 5555),
        "server": ("testserver", 80),
        "state": {},
    }


async def test_lowercase_method_is_state_changing() -> None:
    app = _app()
    ok = await _raw(app, _scope("http", "POST", "/api/echo"))
    assert ok[0]["status"] == 200
    lower = await _raw(app, {**_scope("http", "post", "/api/echo"), "headers": []})
    assert lower[0]["status"] == 403


async def test_websocket_refused() -> None:
    sent = await _raw(_app(), _scope("websocket", "GET", "/api/anything"))
    assert sent == [{"type": "websocket.close", "code": 1008}]

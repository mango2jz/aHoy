"""Browser hardening and request logging, as pure ASGI middleware.

Pure ASGI (not `BaseHTTPMiddleware`) so SSE responses stream untouched and a client
disconnect never cancels the handler.

`AhoyApp.build_middleware_stack` puts these outside Starlette's `ServerErrorMiddleware`, so
the security headers also reach unhandled-exception 500s:

    ProxyHeaders (only with TRUSTED_PROXY_IPS) -> RequestLog -> Security -> FastAPI stack
"""

from __future__ import annotations

import logging
import time
from typing import cast

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn._types import ASGI3Application
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ahoy.config import Settings

request_log = logging.getLogger("ahoy.request")

CSP = (
    "default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self' https://github.com"
)
HSTS = "max-age=63072000; includeSubDomains"
CSRF_HEADER = b"x-ahoy-csrf"
CSRF_EXEMPT_PATHS = frozenset({"/webhooks/github"})
# Everything else (POST, PUT, PATCH, DELETE and any unusual method) is state-changing.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_CSRF_BODY = b'{"detail":"csrf"}'


def security_headers(settings: Settings) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"content-security-policy", CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
    ]
    if settings.ahoy_env == "prod":
        headers.append((b"strict-transport-security", HSTS.encode()))
    return headers


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [v for k, v in scope["headers"] if k.lower() == name]


def csrf_ok(scope: Scope, app_origin: str) -> bool:
    """True if the request may run: a safe method, the webhook, or Origin + X-Ahoy-CSRF."""
    if scope["method"] in SAFE_METHODS or scope["path"] in CSRF_EXEMPT_PATHS:
        return True
    origin = _header_values(scope, b"origin")
    token = _header_values(scope, CSRF_HEADER)
    return origin == [app_origin.encode("latin-1")] and token == [b"1"]


class SecurityMiddleware:
    """Rejects state-changing requests without Origin + X-Ahoy-CSRF (403) before the app
    runs, and adds the security headers to every HTTP response that lacks them."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.app_origin = settings.app_origin
        self.headers = security_headers(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # No WebSocket routes exist (SSE only); refuse before the handshake (HTTP 403).
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in self.headers if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        if not csrf_ok(scope, self.app_origin):
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_CSRF_BODY)).encode()),
                    ],
                }
            )
            await send_with_headers({"type": "http.response.body", "body": _CSRF_BODY})
            return
        await self.app(scope, receive, send_with_headers)


UNMATCHED = "<unmatched>"


def _route_of(scope: Scope) -> str:
    """The matched route template (`/api/sessions/{id}`), else `<unmatched>` (404s, CSRF
    403s before routing). Never the raw path or the query."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) else UNMATCHED


def _user_of(scope: Scope) -> str:
    """The signed-in user id, if the auth dependency recorded one on `request.state`."""
    state = scope.get("state")
    user_id = state.get("user_id") if isinstance(state, dict) else None
    return str(user_id) if user_id is not None else "-"


class RequestLogMiddleware:
    """One log line per HTTP request: user id, method, route, status, latency. Nothing else:
    no headers, no query string, no body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            request_log.info(
                "request user=%s method=%s route=%s status=%d latency_ms=%d",
                _user_of(scope),
                scope["method"],
                _route_of(scope),
                status,
                round((time.perf_counter() - start) * 1000),
            )


def trusted_proxies(settings: Settings) -> list[str]:
    raw = settings.trusted_proxy_ips or ""
    return [ip.strip() for ip in raw.split(",") if ip.strip()]


def wrap(app: ASGIApp, settings: Settings) -> ASGIApp:
    """The outer middleware layers, outermost first: proxy headers, request log, security."""
    wrapped: ASGIApp = RequestLogMiddleware(SecurityMiddleware(app, settings))
    proxies = trusted_proxies(settings)
    if proxies:  # empty = trust none: X-Forwarded-* is never read
        wrapped = cast(
            ASGIApp, ProxyHeadersMiddleware(cast(ASGI3Application, wrapped), trusted_hosts=proxies)
        )
    return wrapped


class AhoyApp(FastAPI):
    """FastAPI with aHoy's security layers outside the whole middleware stack."""

    ahoy_settings: Settings

    def build_middleware_stack(self) -> ASGIApp:
        return wrap(super().build_middleware_stack(), self.ahoy_settings)

"""The one HTTP client every GitHub call goes through.

It sets a timeout, backs off on GitHub's rate-limit signals (`Retry-After`, and
`X-RateLimit-Remaining: 0` with `X-RateLimit-Reset`) within a bound, and makes sure no
credential ever appears in an exception or a log line: errors are rebuilt from the
method, the URL path and GitHub's `message` field, then passed through `redact()`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

log = logging.getLogger("ahoy.github")

API_URL = "https://api.github.com"
WEB_URL = "https://github.com"
API_VERSION = "2022-11-28"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*)[^\r\n,;\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]+"), "gh_[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
)


def redact(text: str, *secrets: SecretStr | str | None) -> str:
    """Remove credentials from `text`: the given secrets verbatim, then known token shapes.

    Values shorter than 8 characters are not replaced verbatim (they would shred ordinary
    words); real tokens, codes and secrets are far longer.
    """
    for secret in secrets:
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class GitHubError(Exception):
    """Base for every GitHub failure. The message is always redacted."""


class GitHubHTTPError(GitHubError):
    """GitHub answered with an unexpected status, or the request did not complete."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubRateLimitedError(GitHubHTTPError):
    """Still rate limited after the bounded wait. `retry_after` is in seconds."""

    def __init__(self, message: str, *, status: int, retry_after: float) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class GitHubAuthError(GitHubError):
    """The OAuth code exchange or token refresh was refused."""


Sleep = Callable[[float], Awaitable[None]]


def bearer(token: SecretStr) -> str:
    return f"Bearer {token.get_secret_value()}"


def basic(user: str, password: SecretStr) -> str:
    raw = f"{user}:{password.get_secret_value()}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class GitHubHTTP:
    """A thin wrapper over one `httpx.AsyncClient`. Credentials are per request, never on
    the client, so one instance serves the App, every user and the OAuth endpoints."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        max_wait_s: float = 60.0,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=False
        )
        self._max_retries = max_retries
        self._max_wait_s = max_wait_s
        self._sleep = sleep
        self._clock = clock

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        auth: SecretStr | None = None,
        secrets: tuple[SecretStr | str | None, ...] = (),
        accept: str = "application/vnd.github+json",
        params: Mapping[str, str | int] | None = None,
        json: Any = None,
        data: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request, retrying only on rate limits.

        `auth` is the full `Authorization` header value, wrapped so it never reaches a
        repr. `secrets` lists extra values (client secret, tokens in the body) to scrub
        from any error text.
        """
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "ahoy",
        }
        if auth is not None:
            headers["Authorization"] = auth.get_secret_value()
        scrub = (auth, *secrets)
        path = httpx.URL(url).path
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(
                    method, url, headers=headers, params=params, json=json, data=data
                )
            except httpx.HTTPError as exc:
                # `from None`: the original exception holds the request and its headers.
                msg = redact(f"GitHub {method} {path} failed: {type(exc).__name__}: {exc}", *scrub)
                raise GitHubHTTPError(msg) from None
            wait = self._rate_limit_wait(resp)
            if wait is None:
                return resp
            if attempt >= self._max_retries or wait > self._max_wait_s:
                raise GitHubRateLimitedError(
                    f"GitHub {method} {path} rate limited (retry after {wait:.0f}s)",
                    status=resp.status_code,
                    retry_after=wait,
                )
            log.warning("GitHub %s %s rate limited; waiting %.1fs", method, path, wait)
            await self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _rate_limit_wait(self, resp: httpx.Response) -> float | None:
        """Seconds to wait before retrying, or None if this is not a rate-limit response."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                return 60.0
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset = float(resp.headers.get("X-RateLimit-Reset", ""))
            except ValueError:
                return 60.0
            return max(0.0, reset - self._clock()) + 1.0
        if resp.status_code == 429:
            return 60.0
        return None


def error_for(
    resp: httpx.Response, *secrets: SecretStr | str | None, what: str = ""
) -> GitHubHTTPError:
    """A redacted error for an unexpected response: method, path, status, GitHub's message."""
    message = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            # Redact before truncating: a cut through a secret would defeat verbatim matching.
            message = redact(
                str(body.get("message") or body.get("error_description") or ""), *secrets
            )[:200]
    except ValueError:
        pass
    req = resp.request
    text = f"GitHub {req.method} {req.url.path} returned {resp.status_code}"
    if what:
        text = f"{what}: {text}"
    if message:
        text += f": {message}"
    return GitHubHTTPError(redact(text, *secrets), status=resp.status_code)

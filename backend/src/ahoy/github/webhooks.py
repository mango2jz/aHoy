"""`POST /webhooks/github` (SPEC -> GitHub integration, Webhooks).

Webhooks only make revocation faster; `authorize` re-checks with GitHub at least every
5 minutes anyway. The order of checks:

1. Read the raw body, at most MAX_BODY_BYTES (else 413), before anything is parsed.
2. `X-Hub-Signature-256` must be `sha256=` + hex HMAC-SHA256(GITHUB_WEBHOOK_SECRET, body),
   compared in constant time. Missing, malformed or wrong -> 401; with no secret configured
   every request gets 401 (an unsigned webhook is never accepted).
3. `X-GitHub-Delivery` and `X-GitHub-Event` must be present and well formed (else 400), and
   the body must be a JSON object (else 400).
4. Handled events are deduplicated by delivery id in `webhook_deliveries`: a replay answers
   `{"status": "duplicate"}` and does nothing. The id is recorded in the same transaction as
   the database side effects, so a delivery whose handling fails is not marked seen and a
   redelivery is processed. Cache invalidation (in memory) runs after that commit.
   Unhandled events and actions answer `{"status": "ignored"}` and are not recorded.
5. Every `installation` and `installation_repositories` event (any action, replays too)
   clears the 60 s repo listing cache (`app.state.repo_list`); a revoked authorization
   drops that user's listing.

CSRF does not apply here (security.py exempts exactly this path); the signature is the
authentication. Only the event, action and delivery id are logged, never the payload or
the signature.
"""

import hashlib
import hmac
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ahoy.auth.sessions import AuthState, get_auth
from ahoy.authz import access_cache
from ahoy.store import webhooks as store

log = logging.getLogger(__name__)

router = APIRouter()

MAX_BODY_BYTES = 5 * 1024 * 1024
SIGNATURE_PREFIX = "sha256="
_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}")
_DELIVERY = re.compile(r"[0-9A-Za-z-]{1,64}")
_EVENT = re.compile(r"[a-z_]{1,64}")
_ACTION = re.compile(r"[a-z_]{1,64}")


def _unauthorized() -> HTTPException:
    return HTTPException(401, "invalid signature")


def _bad_request(detail: str = "malformed webhook") -> HTTPException:
    return HTTPException(400, detail)


async def _read_body(request: Request) -> bytes:
    """The raw body, refusing (413) anything over MAX_BODY_BYTES without reading it all."""
    length = request.headers.get("content-length")
    if (
        length is not None
        and length.isascii()
        and length.isdigit()
        and int(length) > MAX_BODY_BYTES
    ):
        raise HTTPException(413, "payload too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "payload too large")
    return bytes(body)


def signature_ok(secret: bytes, body: bytes, header: str | None) -> bool:
    if header is None or not _SIGNATURE.fullmatch(header):
        return False
    expected = SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


def _header(request: Request, name: str, pattern: re.Pattern[str]) -> str:
    value = request.headers.get(name)
    if value is None or not pattern.fullmatch(value):
        raise _bad_request(f"missing or malformed {name}")
    return value


def _id(obj: Any) -> int:
    """`obj["id"]` as a positive int, else 400."""
    value = obj.get("id") if isinstance(obj, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _bad_request()
    return value


def _ids(items: Any) -> list[int]:
    if not isinstance(items, list):
        raise _bad_request()
    return [_id(item) for item in items]


@router.post("/webhooks/github", include_in_schema=False)
async def github_webhook(request: Request) -> dict[str, str]:
    auth = get_auth(request)
    body = await _read_body(request)
    secret = auth.settings.github_webhook_secret
    if secret is None or not secret.get_secret_value():
        raise _unauthorized()
    if not signature_ok(
        secret.get_secret_value().encode(), body, request.headers.get("x-hub-signature-256")
    ):
        raise _unauthorized()

    delivery = _header(request, "x-github-delivery", _DELIVERY)
    event = _header(request, "x-github-event", _EVENT)
    try:
        payload = json.loads(body)
    except ValueError:
        raise _bad_request("body is not JSON") from None
    if not isinstance(payload, dict):
        raise _bad_request()
    raw_action = payload.get("action")
    action = raw_action if isinstance(raw_action, str) and _ACTION.fullmatch(raw_action) else ""
    log.info("github webhook event=%s action=%s delivery=%s", event, action, delivery)
    listings = getattr(request.app.state, "repo_list", None)  # GET /api/repos cache
    if listings is not None and event in ("installation", "installation_repositories"):
        # Any action (added, removed, created, suspended...) can change who sees which
        # repo; dropping 60 s listings is cheap and idempotent, so replays do it too.
        listings.clear()

    if event == "installation" and action in ("deleted", "suspend"):
        installation_id = _id(payload.get("installation"))
        repos = payload.get("repositories")
        repo_ids = None if repos is None else _ids(repos)  # also via other installations
        cache = access_cache(auth)
        if not await _record(auth, delivery):
            return _duplicate(event, action, delivery)
        cache.invalidate_installation(installation_id)
        for repo_id in repo_ids or ():
            cache.invalidate(repo_id)
        return {"status": "ok"}

    if event == "installation_repositories" and action == "removed":
        repo_ids = _ids(payload.get("repositories_removed"))
        cache = access_cache(auth)
        if not await _record(auth, delivery):
            return _duplicate(event, action, delivery)
        for repo_id in repo_ids:
            cache.invalidate(repo_id)
        return {"status": "ok"}

    if event == "github_app_authorization" and action == "revoked":
        github_id = _id(payload.get("sender"))
        cache = access_cache(auth)
        if auth.sessionmaker is None:
            raise HTTPException(503, "database not configured")
        async with auth.sessionmaker() as db:
            result = await store.record_revocation(db, delivery, github_id)
        if not result.new:
            return _duplicate(event, action, delivery)
        if result.user_id is not None:
            cache.invalidate_user(result.user_id)
            if listings is not None:
                listings.invalidate_user(result.user_id)
        return {"status": "ok"}

    return {"status": "ignored"}


async def _record(auth: AuthState, delivery: str) -> bool:
    if auth.sessionmaker is None:
        raise HTTPException(503, "database not configured")
    async with auth.sessionmaker() as db:
        return await store.record_delivery(db, delivery)


def _duplicate(event: str, action: str, delivery: str) -> dict[str, str]:
    log.info("github webhook duplicate event=%s action=%s delivery=%s", event, action, delivery)
    return {"status": "duplicate"}

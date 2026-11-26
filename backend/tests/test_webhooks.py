"""T25: `POST /webhooks/github` (SPEC -> GitHub integration, Webhooks; success criterion 9).

The real app (mock provider + mock GitHub) with a configured webhook secret; bodies are
signed with it. No request sends the CSRF headers: the webhook is exempt.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy import authz
from ahoy.app import create_app
from ahoy.auth import crypto, sessions
from ahoy.auth.sessions import AuthState
from ahoy.authz import AccessCache, Action
from ahoy.config import Settings
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.github.webhooks import MAX_BODY_BYTES
from ahoy.harness.schemas import Session, User
from ahoy.store import auth as auth_store
from ahoy.store.models import AuthSessionRow, WebhookDeliveryRow
from conftest import MakeSession, SameSession

pytestmark = pytest.mark.db

ORIGIN = "http://127.0.0.1:5173"
SECRET = "webhook-test-secret"
DEMO = 7001  # private, alice + bob can read it


def _settings(secret: str | None = SECRET) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        ahoy_env="test",
        database_url=None,
        app_origin=ORIGIN,
        token_encryption_key=crypto.generate_key(),
        github_webhook_secret=secret,
    )


def _make_app(db: AsyncSession, secret: str | None = SECRET) -> FastAPI:
    app = create_app(_settings(secret), "mock", "mock")

    async def override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[sessions.get_db] = override
    app.state.auth.sessionmaker = SameSession(db).maker()
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Hook:
    """The app, a client, and helpers to send signed deliveries and sign users in."""

    def __init__(self, app: FastAPI, client: httpx.AsyncClient, db: AsyncSession) -> None:
        self.app = app
        self.client = client
        self.db = db
        self.auth: AuthState = app.state.auth
        self.gh = cast(MockGitHub, self.auth.github)
        self.cache = cast(AccessCache, self.auth.access)
        self.clients: list[httpx.AsyncClient] = []

    async def send(
        self,
        event: str,
        payload: Any,
        *,
        delivery: str | None = None,
        signature: str | None = "auto",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        raw = json.dumps(payload).encode() if body is None else body
        sent = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery or str(uuid4())}
        if signature == "auto":
            sent["X-Hub-Signature-256"] = sign(raw)
        elif signature is not None:
            sent["X-Hub-Signature-256"] = signature
        sent |= headers or {}
        return await self.client.post("/webhooks/github", content=raw, headers=sent)

    async def sign_in(self, name: str) -> tuple[httpx.AsyncClient, User]:
        client = _client(self.app)
        self.clients.append(client)
        resp = await client.get("/auth/mock/login", params={"user": name})
        assert resp.status_code == 303, resp.text
        me = (await client.get("/api/me")).json()
        user = await auth_store.get_user(self.db, UUID(me["id"]))
        assert user is not None
        return client, user

    async def view(self, user: User, session: Session) -> int:
        try:
            await authz.load_session(self.auth, self.db, user, session.id, Action.VIEW)
        except HTTPException as exc:
            return exc.status_code
        return 200

    async def login_sessions(self, user: User) -> int:
        stmt = select(func.count()).where(AuthSessionRow.user_id == user.id)
        return int((await self.db.execute(stmt)).scalar_one())

    async def seen(self, delivery: str) -> int:
        stmt = select(func.count()).where(WebhookDeliveryRow.delivery_id == delivery)
        return int((await self.db.execute(stmt)).scalar_one())


@pytest.fixture
async def hook(db: AsyncSession) -> AsyncIterator[Hook]:
    app = _make_app(db)
    async with _client(app) as client:
        hook = Hook(app, client, db)
        try:
            yield hook
        finally:
            for signed_in in hook.clients:
                await signed_in.aclose()


def _removed(*repo_ids: int) -> dict[str, Any]:
    return {
        "action": "removed",
        "installation": {"id": INSTALLATION_ID},
        "repositories_removed": [{"id": r, "full_name": f"x/{r}"} for r in repo_ids],
    }


# --- signature, size, headers ---------------------------------------------------------


async def test_valid_signature_unknown_event_is_ignored(hook: Hook) -> None:
    resp = await hook.send("push", {"ref": "refs/heads/main"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}


async def test_unknown_action_is_ignored(hook: Hook) -> None:
    resp = await hook.send("installation", {"action": "created", "installation": {"id": 1}})
    assert resp.json() == {"status": "ignored"}


@pytest.mark.parametrize(
    "signature",
    [
        None,  # missing
        "",
        "sha256=" + "0" * 64,  # wrong
        sign(b"other body"),  # signed a different body
        sign(b"{}", "wrong-secret"),
        "sha1=" + "0" * 40,  # malformed
        "sha256=" + "A" * 64,  # not lowercase hex
        "sha256=" + "0" * 63,
    ],
)
async def test_bad_or_missing_signature_401(hook: Hook, signature: str | None) -> None:
    resp = await hook.send("installation_repositories", _removed(DEMO), signature=signature)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid signature"}


async def test_bad_signature_has_no_side_effects(hook: Hook, make_session: MakeSession) -> None:
    _bob_client, bob = await hook.sign_in("bob")
    _alice_client, alice = await hook.sign_in("alice")
    session = await make_session(alice, shared=True)
    assert await hook.view(bob, session) == 200
    delivery = str(uuid4())
    resp = await hook.send(
        "installation_repositories", _removed(DEMO), delivery=delivery, signature=sign(b"x")
    )
    assert resp.status_code == 401
    assert len(hook.cache) == 1
    assert await hook.seen(delivery) == 0


async def test_no_secret_configured_always_401(db: AsyncSession) -> None:
    for secret in (None, ""):
        app = _make_app(db, secret)
        async with _client(app) as client:
            hook = Hook(app, client, db)
            body = json.dumps({"action": "removed"}).encode()
            for signature in (None, sign(body), sign(body, "")):
                resp = await hook.send(
                    "installation_repositories", None, body=body, signature=signature
                )
                assert resp.status_code == 401


async def test_oversized_body_413(hook: Hook) -> None:
    body = b"x" * (MAX_BODY_BYTES + 1)
    resp = await hook.send("push", None, body=body)
    assert resp.status_code == 413
    # Without a Content-Length (chunked), the streamed read stops at the cap too.

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(6):
            yield b"x" * (1024 * 1024)

    resp = await hook.client.post(
        "/webhooks/github",
        content=chunks(),
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": str(uuid4())},
    )
    assert resp.status_code == 413


async def test_non_ascii_digit_content_length_is_not_a_500(hook: Hook) -> None:
    """ "\u00b2" (latin-1 byte 0xb2) passes str.isdigit() but not int(); raw ASGI, since
    httpx would set its own Content-Length."""
    body = b"{}"
    headers = [
        (b"content-length", b"\xb2"),
        (b"x-github-event", b"push"),
        (b"x-github-delivery", str(uuid4()).encode()),
        (b"x-hub-signature-256", sign(body).encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 5555),
        "server": ("testserver", 80),
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await hook.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert b'"ignored"' in b"".join(m.get("body", b"") for m in sent)


async def test_body_at_cap_is_read(hook: Hook) -> None:
    body = b" " * (MAX_BODY_BYTES - 2) + b"{}"
    resp = await hook.send("push", None, body=body)
    assert resp.json() == {"status": "ignored"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-GitHub-Delivery": ""},
        {"X-GitHub-Delivery": "x" * 65},
        {"X-GitHub-Delivery": "not a uuid!"},
        {"X-GitHub-Event": ""},
        {"X-GitHub-Event": "Push"},
    ],
)
async def test_malformed_headers_400(hook: Hook, headers: dict[str, str]) -> None:
    resp = await hook.send("push", {}, headers=headers)
    assert resp.status_code == 400


async def test_missing_headers_400(hook: Hook) -> None:
    body = b"{}"
    for missing in ("X-GitHub-Delivery", "X-GitHub-Event"):
        sent = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": str(uuid4()),
            "X-Hub-Signature-256": sign(body),
        }
        del sent[missing]
        resp = await hook.client.post("/webhooks/github", content=body, headers=sent)
        assert resp.status_code == 400


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"\xff\xfe", b""])
async def test_malformed_json_after_valid_signature_400(hook: Hook, body: bytes) -> None:
    resp = await hook.send("installation_repositories", None, body=body)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("installation_repositories", {"action": "removed"}),
        ("installation_repositories", {"action": "removed", "repositories_removed": [{}]}),
        ("installation_repositories", {"action": "removed", "repositories_removed": [{"id": 0}]}),
        ("installation", {"action": "deleted"}),
        ("installation", {"action": "deleted", "installation": {"id": "1"}}),
        ("installation", {"action": "suspend", "installation": {"id": 1}, "repositories": {}}),
        ("github_app_authorization", {"action": "revoked", "sender": {"id": True}}),
    ],
)
async def test_malformed_payload_400_and_not_marked_seen(
    hook: Hook, event: str, payload: dict[str, Any]
) -> None:
    delivery = str(uuid4())
    assert (await hook.send(event, payload, delivery=delivery)).status_code == 400
    assert await hook.seen(delivery) == 0


async def test_csrf_headers_not_required(hook: Hook) -> None:
    resp = await hook.send("push", {}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200


# --- events ---------------------------------------------------------------------------


async def test_installation_repositories_removed_hides_session_immediately(
    hook: Hook, make_session: MakeSession
) -> None:
    """Success criterion 9: no clock advance; the cached True answer is dropped."""
    _alice_client, alice = await hook.sign_in("alice")
    _bob_client, bob = await hook.sign_in("bob")
    session = await make_session(alice, shared=True)
    assert await hook.view(bob, session) == 200
    assert await hook.view(alice, session) == 200

    hook.gh.remove_from_installation(DEMO)
    assert await hook.view(bob, session) == 200  # still cached: only the webhook is fast
    delivery = str(uuid4())
    resp = await hook.send("installation_repositories", _removed(DEMO), delivery=delivery)
    assert resp.json() == {"status": "ok"}
    assert await hook.seen(delivery) == 1
    assert await hook.view(bob, session) == 404
    assert await hook.view(alice, session) == 404


async def test_replayed_delivery_has_side_effects_once(
    hook: Hook, make_session: MakeSession
) -> None:
    _alice_client, alice = await hook.sign_in("alice")
    session = await make_session(alice)
    calls: list[int] = []
    original = hook.cache.invalidate

    def spy(repo_id: int) -> None:
        calls.append(repo_id)
        original(repo_id)

    hook.cache.invalidate = spy  # type: ignore[method-assign]
    delivery = str(uuid4())
    first = await hook.send("installation_repositories", _removed(DEMO, 7002), delivery=delivery)
    assert first.json() == {"status": "ok"}
    assert calls == [DEMO, 7002]
    assert await hook.view(alice, session) == 200  # cached again
    replay = await hook.send("installation_repositories", _removed(DEMO), delivery=delivery)
    assert replay.status_code == 200
    assert replay.json() == {"status": "duplicate"}
    assert calls == [DEMO, 7002]
    assert len(hook.cache) == 1
    assert await hook.seen(delivery) == 1


@pytest.mark.parametrize("action", ["deleted", "suspend"])
async def test_installation_with_repositories_invalidates_them(
    hook: Hook, make_session: MakeSession, action: str
) -> None:
    """The whole installation is dropped, plus each listed repo under any installation."""
    _alice_client, alice = await hook.sign_in("alice")
    other = INSTALLATION_ID + 1
    sessions = [
        await make_session(alice, repo_id=repo_id, installation_id=inst)
        for repo_id in (DEMO, 7002)
        for inst in (INSTALLATION_ID, other)
    ]
    for session in sessions:
        await hook.view(alice, session)
    assert len(hook.cache) == 4
    payload = {
        "action": action,
        "installation": {"id": INSTALLATION_ID},
        "repositories": [{"id": DEMO}],
    }
    assert (await hook.send("installation", payload)).json() == {"status": "ok"}
    assert hook.cache.cached((alice.id, DEMO, INSTALLATION_ID)) is None
    assert hook.cache.cached((alice.id, 7002, INSTALLATION_ID)) is None  # unlisted, same inst
    assert hook.cache.cached((alice.id, DEMO, other)) is None  # listed, other installation
    assert hook.cache.cached((alice.id, 7002, other)) is False  # neither: kept
    assert len(hook.cache) == 1


@pytest.mark.parametrize("action", ["deleted", "suspend"])
async def test_installation_without_repositories_invalidates_installation(
    hook: Hook, make_session: MakeSession, action: str
) -> None:
    _alice_client, alice = await hook.sign_in("alice")
    demo = await make_session(alice, repo_id=DEMO)
    other = await make_session(alice, repo_id=DEMO, installation_id=INSTALLATION_ID + 1)
    assert await hook.view(alice, demo) == 200
    assert await hook.view(alice, other) == 404  # cached False, other installation
    assert len(hook.cache) == 2
    payload = {"action": action, "installation": {"id": INSTALLATION_ID}}
    assert (await hook.send("installation", payload)).json() == {"status": "ok"}
    assert hook.cache.cached((alice.id, DEMO, INSTALLATION_ID)) is None
    assert hook.cache.cached((alice.id, DEMO, INSTALLATION_ID + 1)) is False


async def test_app_authorization_revoked_signs_user_out(
    hook: Hook, make_session: MakeSession
) -> None:
    alice_client, alice = await hook.sign_in("alice")
    bob_client, bob = await hook.sign_in("bob")
    bob_other, _ = await hook.sign_in("bob")  # a second login session (second cookie)
    session = await make_session(alice, shared=True)
    assert await hook.view(bob, session) == 200
    assert await hook.view(alice, session) == 200
    assert await hook.login_sessions(bob) == 2
    for client in (bob_client, bob_other):
        assert (await client.get("/api/me")).status_code == 200

    payload = {"action": "revoked", "sender": {"id": bob.github_id, "login": bob.github_login}}
    delivery = str(uuid4())
    resp = await hook.send("github_app_authorization", payload, delivery=delivery)
    assert resp.json() == {"status": "ok"}
    assert await hook.login_sessions(bob) == 0
    for client in (bob_client, bob_other):
        assert (await client.get("/api/me")).status_code == 401
    assert await auth_store.get_tokens(hook.db, hook.auth.key, bob.id) is None
    assert hook.cache.cached((bob.id, DEMO, INSTALLATION_ID)) is None
    assert hook.cache.cached((alice.id, DEMO, INSTALLATION_ID)) is True
    assert (await alice_client.get("/api/me")).status_code == 200

    # A replay does nothing: bob signs in again and stays signed in.
    bob_again, _ = await hook.sign_in("bob")
    replay = await hook.send("github_app_authorization", payload, delivery=delivery)
    assert replay.json() == {"status": "duplicate"}
    assert (await bob_again.get("/api/me")).status_code == 200


async def test_app_authorization_revoked_unknown_user(hook: Hook) -> None:
    delivery = str(uuid4())
    payload = {"action": "revoked", "sender": {"id": 987_654_321}}
    resp = await hook.send("github_app_authorization", payload, delivery=delivery)
    assert resp.json() == {"status": "ok"}
    assert await hook.seen(delivery) == 1


async def test_get_user_by_github_id(hook: Hook) -> None:
    _, bob = await hook.sign_in("bob")
    assert await auth_store.get_user_by_github_id(hook.db, bob.github_id) == bob
    assert await auth_store.get_user_by_github_id(hook.db, 987_654_321) is None


async def test_logs_never_hold_payload_or_signature(
    hook: Hook, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = _removed(DEMO) | {"secret_marker": "PAYLOAD-MARKER"}
    body = json.dumps(payload).encode()
    delivery = str(uuid4())
    await hook.send("installation_repositories", None, body=body, delivery=delivery)
    text = caplog.text
    assert delivery in text and "installation_repositories" in text and "removed" in text
    assert "PAYLOAD-MARKER" not in text
    assert sign(body).removeprefix("sha256=") not in text
    assert SECRET not in text


# --- the repo listing cache (GET /api/repos) ------------------------------------------


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("installation", {"action": "deleted", "installation": {"id": INSTALLATION_ID}}),
        ("installation", {"action": "created", "installation": {"id": INSTALLATION_ID}}),
        ("installation_repositories", _removed(DEMO)),
        ("installation_repositories", {"action": "added", "repositories_added": []}),
    ],
)
async def test_installation_events_clear_repo_listings(
    hook: Hook, event: str, payload: dict[str, Any]
) -> None:
    alice_client, _ = await hook.sign_in("alice")
    bob_client, _ = await hook.sign_in("bob")
    for client in (alice_client, bob_client):
        assert (await client.get("/api/repos")).status_code == 200
    listings = hook.app.state.repo_list
    assert len(listings) == 2
    resp = await hook.send(event, payload)
    assert resp.status_code == 200
    assert len(listings) == 0


async def test_unrelated_event_keeps_repo_listings(hook: Hook) -> None:
    alice_client, _ = await hook.sign_in("alice")
    await alice_client.get("/api/repos")
    await hook.send("push", {"ref": "refs/heads/main"})
    assert len(hook.app.state.repo_list) == 1


async def test_app_authorization_revoked_drops_that_users_listing(hook: Hook) -> None:
    alice_client, _ = await hook.sign_in("alice")
    bob_client, bob = await hook.sign_in("bob")
    for client in (alice_client, bob_client):
        await client.get("/api/repos")
    payload = {"action": "revoked", "sender": {"id": bob.github_id}}
    assert (await hook.send("github_app_authorization", payload)).json() == {"status": "ok"}
    listings = hook.app.state.repo_list
    assert len(listings) == 1 and bob.id not in listings._entries

"""GitHub sign-in (SPEC -> Authentication -> Sign-in flow), the web flow with PKCE.

`GET /auth/github/login` puts a random `state` and a PKCE verifier into a signed, 10-minute,
HttpOnly pre-auth cookie and redirects to GitHub's authorize page with the S256 challenge.
`GET /auth/github/callback` checks `state` against that cookie (constant time), exchanges
the code with the verifier, reads the user and hands off to `sessions.start`.

The cookie value is `base64url(json{state, verifier, exp}) "." base64url(HMAC-SHA256)`,
keyed with an HKDF subkey of TOKEN_ENCRYPTION_KEY. `exp` is checked here as well as by the
browser's Max-Age. Every failure answers with the same generic 400 and clears the cookie.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.auth import crypto, sessions
from ahoy.auth.sessions import Auth, AuthState, preauth_cookie_name
from ahoy.config import Settings
from ahoy.github.http import GitHubAuthError, GitHubError
from ahoy.github.user_client import UserTokens
from ahoy.store import auth as auth_store

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
LOGIN_PATH = "/auth/github/login"
CALLBACK_PATH = "/auth/github/callback"
PREAUTH_TTL_S = 600
PREAUTH_KEY_INFO = b"ahoy-preauth"
STATE_BYTES = 32
VERIFIER_BYTES = 48  # token_urlsafe(48) is 64 characters (PKCE allows 43-128)

router = APIRouter()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    """Strict, canonical unpadded base64url: junk characters, padding or non-zero trailing
    bits raise ValueError, so each signature has exactly one accepted encoding."""
    decoded = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    if _b64(decoded) != text:
        raise ValueError("non-canonical base64url")
    return decoded


def code_challenge(verifier: str) -> str:
    """The PKCE S256 challenge for `verifier`."""
    return _b64(hashlib.sha256(verifier.encode()).digest())


def redirect_uri(settings: Settings) -> str:
    return settings.app_origin.rstrip("/") + CALLBACK_PATH


def _mac(auth: AuthState, payload: str) -> bytes:
    key = crypto.derive_subkey(auth.key, PREAUTH_KEY_INFO)
    return hmac.new(key, payload.encode(), hashlib.sha256).digest()


def encode_preauth(auth: AuthState, state: str, verifier: str, exp: int) -> str:
    body = json.dumps({"state": state, "verifier": verifier, "exp": exp}, separators=(",", ":"))
    payload = _b64(body.encode())
    return f"{payload}.{_b64(_mac(auth, payload))}"


def decode_preauth(auth: AuthState, value: str | None) -> tuple[str, str] | None:
    """(state, verifier) from a valid, unexpired cookie value; None otherwise."""
    if not value or value.count(".") != 1:
        return None
    payload, sig = value.split(".")
    try:
        given = _unb64(sig)
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(given, _mac(auth, payload)):
        return None
    try:
        data: Any = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state, verifier, exp = data.get("state"), data.get("verifier"), data.get("exp")
    if not (isinstance(state, str) and state and isinstance(verifier, str) and verifier):
        return None
    if not isinstance(exp, int) or isinstance(exp, bool) or exp <= auth.now().timestamp():
        return None
    return state, verifier


def _secure(settings: Settings) -> bool:
    return settings.ahoy_env == "prod"


def _clear_preauth(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        preauth_cookie_name(settings),
        path="/",
        secure=_secure(settings),
        httponly=True,
        samesite="lax",
    )


def _failed(settings: Settings, status: int = 400) -> Response:
    """A generic failure (no detail about which check failed) that drops the cookie."""
    detail = "sign-in failed, start again" if status == 400 else "sign-in unavailable, try again"
    response = JSONResponse({"detail": detail}, status_code=status)
    _clear_preauth(response, settings)
    return response


async def complete_sign_in(request: Request, db: AsyncSession, tokens: UserTokens) -> Response:
    """Read the GitHub user, upsert them by numeric id and start a login session. Shared by
    the callback and the dev/test mock login. GitHub errors propagate to the caller."""
    auth = sessions.get_auth(request)
    info = await auth.github.get_user(tokens.access_token)
    user = await auth_store.upsert_user(db, info, default_model=auth.default_model)
    response = await sessions.start(request, db, user, tokens)
    if auth.access is not None:
        # A fresh sign-in (e.g. right after installing the App) must not keep seeing a
        # cached denial; checks already running for this user are not stored either.
        auth.access.invalidate_user(user.id)
    listings = getattr(request.app.state, "repo_list", None)  # GET /api/repos cache (60 s)
    if listings is not None:
        listings.invalidate_user(user.id)  # a just-installed repo shows at once
    return response


@router.get(LOGIN_PATH)
async def login(auth: Auth) -> Response:
    settings = auth.settings
    state = secrets.token_urlsafe(STATE_BYTES)
    verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    exp = int(auth.now().timestamp()) + PREAUTH_TTL_S
    query = urlencode(
        {
            "client_id": settings.github_client_id or "",
            "redirect_uri": redirect_uri(settings),
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=302)
    response.set_cookie(
        preauth_cookie_name(settings),
        encode_preauth(auth, state, verifier, exp),
        max_age=PREAUTH_TTL_S,
        path="/",
        secure=_secure(settings),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get(CALLBACK_PATH)
async def callback(
    request: Request,
    auth: Auth,
    code: str | None = None,
    state: str | None = None,
    setup_action: str | None = None,
) -> Response:
    settings = auth.settings
    if setup_action is not None and state is None:
        # Straight from the install page: we issued no state, so the code is untrusted and
        # never exchanged. A fresh login completes silently (the App was just authorized).
        return RedirectResponse(LOGIN_PATH, status_code=302)
    preauth = decode_preauth(auth, request.cookies.get(preauth_cookie_name(settings)))
    if preauth is None or not state or not code:
        return _failed(settings)
    expected, verifier = preauth
    if not hmac.compare_digest(state.encode(), expected.encode()):
        return _failed(settings)
    if auth.sessionmaker is None:
        return _failed(settings, 503)
    # The DB is opened only here: the install bounce and rejected requests never touch it.
    try:
        tokens = await auth.github.exchange_code(code, verifier, redirect_uri(settings))
        async with auth.sessionmaker() as db:
            return await complete_sign_in(request, db, tokens)
    except GitHubAuthError:
        return _failed(settings)
    except GitHubError:
        return _failed(settings, 503)

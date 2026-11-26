"""`GET /auth/mock/login?user=<alice|bob|mallory>`: dev/test sign-in without GitHub.

`create_app` includes this router only with `--github mock` and AHOY_ENV dev or test, so in
any other configuration the route does not exist at all. It signs the fixture user in
through the same path as the real callback: a mock code bound to a PKCE challenge is
exchanged for (mock) tokens, which are stored encrypted, then `complete_sign_in`.
"""

import secrets

from fastapi import APIRouter, HTTPException, Request, Response

from ahoy.auth.github_oauth import code_challenge, complete_sign_in, redirect_uri
from ahoy.auth.sessions import DB, Auth
from ahoy.github.mock import MockGitHub

router = APIRouter()


@router.get("/auth/mock/login")
async def mock_login(request: Request, auth: Auth, db: DB, user: str = "") -> Response:
    github = auth.github
    if not isinstance(github, MockGitHub):  # create_app never registers it otherwise
        raise HTTPException(404)
    if user not in github.users:
        raise HTTPException(400, "unknown mock user")
    verifier = secrets.token_urlsafe(48)
    code = github.issue_code(user, code_challenge(verifier))
    tokens = await github.exchange_code(code, verifier, redirect_uri(auth.settings))
    return await complete_sign_in(request, db, tokens)

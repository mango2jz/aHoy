"""Real GitHub clients: request shapes pinned with respx, rate limits, redaction, snapshots."""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from ahoy.github import (
    GitHubAuthError,
    GitHubError,
    GitHubHTTP,
    GitHubHTTPError,
    GitHubRateLimitedError,
    HttpGitHubApp,
    HttpGitHubUser,
    RepoTarget,
    UserTokens,
    app_client,
    make_app_jwt,
    remove_snapshot,
)
from ahoy.github.app_client import SnapshotError
from ahoy.github.http import error_for
from ahoy.github.mock import fixture_bare_repos

API = "https://api.github.com"
WEB = "https://github.com"
SHA = "a" * 40
INSTALL_TOKEN = "ghs_INSTALLcanary0000000000000000000000"
USER_TOKEN = "ghu_USERcanary00000000000000000000000000"
REFRESH_TOKEN = "ghr_REFRESHcanary000000000000000000000000"
CLIENT_SECRET = "client-secret-canary-123456"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[SecretStr, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return SecretStr(private), public


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def make_http(sleep: Callable[[float], Any] | None = None, **kw: Any) -> GitHubHTTP:
    return GitHubHTTP(sleep=sleep or Sleeps(), **kw)


def make_app(rsa_keys: tuple[SecretStr, bytes], **kw: Any) -> HttpGitHubApp:
    return HttpGitHubApp(make_http(), app_id="12345", private_key=rsa_keys[0], **kw)


def make_user(**kw: Any) -> HttpGitHubUser:
    return HttpGitHubUser(
        make_http(**kw), client_id="Iv1.client", client_secret=SecretStr(CLIENT_SECRET)
    )


def token_ok(repo_id: int = 42, perms: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "token": INSTALL_TOKEN,
            "expires_at": "2026-11-25T12:00:00Z",
            "permissions": perms or {"contents": "read", "metadata": "read"},
            "repository_selection": "selected",
            "repositories": [{"id": repo_id}],
        },
    )


REPO = RepoTarget(id=42, installation_id=7)


def repo_lookup(full_name: str = "octo/demo", repo_id: int = 42, **extra: Any) -> respx.Route:
    """GitHub's answer to `GET /repositories/{id}`: the repo's *current* name."""
    body = {"id": repo_id, "full_name": full_name, "default_branch": "trunk", **extra}
    return respx.get(f"{API}/repositories/{repo_id}").mock(
        return_value=httpx.Response(200, json=body)
    )


# --- App JWT and installation tokens ---------------------------------------------------


def test_app_jwt_claims(rsa_keys: tuple[SecretStr, bytes]) -> None:
    now = time.time()
    token = make_app_jwt("12345", rsa_keys[0], now=now).get_secret_value()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, rsa_keys[1], algorithms=["RS256"], options={"verify_iat": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(now) - 60
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["exp"] > int(now)


@respx.mock
def test_installation_token_is_narrowed(rsa_keys: tuple[SecretStr, bytes]) -> None:
    route = respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    token = run(make_app(rsa_keys).installation_token(7, 42))
    assert token.get_secret_value() == INSTALL_TOKEN
    req = route.calls.last.request
    assert json.loads(req.content) == {
        "repository_ids": [42],
        "permissions": {"contents": "read"},
    }
    scheme, app_jwt = req.headers["Authorization"].split(" ", 1)
    assert scheme == "Bearer"
    assert jwt.decode(app_jwt, rsa_keys[1], algorithms=["RS256"])["iss"] == "12345"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.parametrize(
    "resp",
    [
        token_ok(perms={"contents": "write"}),
        token_ok(perms={"contents": "read", "issues": "read"}),
        token_ok(repo_id=99),
        # No `repositories` list: not narrowed to one repo, so fail closed.
        httpx.Response(
            201,
            json={
                "token": INSTALL_TOKEN,
                "permissions": {"contents": "read"},
                "repository_selection": "all",
            },
        ),
        httpx.Response(
            201,
            json={
                "token": INSTALL_TOKEN,
                "permissions": {"contents": "read"},
                "repositories": ["junk"],
            },
        ),
    ],
)
@respx.mock
def test_broader_installation_token_refused(
    rsa_keys: tuple[SecretStr, bytes], resp: httpx.Response
) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=resp)
    with pytest.raises(GitHubError, match="broader") as exc:
        run(make_app(rsa_keys).installation_token(7, 42))
    assert INSTALL_TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    ("status", "expected"), [(201, True), (403, False), (404, False), (422, False)]
)
@respx.mock
def test_can_mint_for(rsa_keys: tuple[SecretStr, bytes], status: int, expected: bool) -> None:
    resp = token_ok() if status == 201 else httpx.Response(status, json={"message": "nope"})
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=resp)
    assert run(make_app(rsa_keys).can_mint_for(7, 42)) is expected


@respx.mock
def test_can_mint_for_rate_limited_is_not_no_access(rsa_keys: tuple[SecretStr, bytes]) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
        )
    )
    with pytest.raises(GitHubRateLimitedError):
        run(make_app(rsa_keys).can_mint_for(7, 42))


@respx.mock
def test_can_read_repo_rate_limited_is_not_no_access() -> None:
    respx.get(f"{API}/repositories/42").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    with pytest.raises(GitHubRateLimitedError):
        run(make_user().can_read_repo(SecretStr(USER_TOKEN), 42))


@respx.mock
def test_renamed_repo_still_resolves(rsa_keys: tuple[SecretStr, bytes]) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    lookup = repo_lookup("octo/renamed")
    renamed = respx.get(f"{API}/repos/octo/renamed/commits/main").mock(
        return_value=httpx.Response(200, text=SHA)
    )
    assert run(make_app(rsa_keys).resolve_ref(REPO, "main")) == SHA
    assert renamed.called
    assert lookup.calls.last.request.headers["Authorization"] == f"Bearer {INSTALL_TOKEN}"


@respx.mock
def test_name_now_owned_by_another_repo_is_never_used(rsa_keys: tuple[SecretStr, bytes]) -> None:
    # Repo 42 used to be "octo/demo"; that name now belongs to repo 99.
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    repo_lookup("octo/moved")
    stale = respx.route(url__startswith=f"{API}/repos/octo/demo").mock(
        return_value=httpx.Response(200, text="f" * 40)
    )
    respx.get(f"{API}/repos/octo/moved/commits/trunk").mock(
        return_value=httpx.Response(200, text=SHA)
    )
    respx.get(f"{API}/repos/octo/moved/git/trees/{SHA}").mock(
        return_value=httpx.Response(200, json={"sha": SHA, "truncated": False, "tree": []})
    )
    app = make_app(rsa_keys)
    assert run(app.resolve_ref(REPO, None)) == SHA
    run(app.tree(REPO, SHA))
    assert not stale.called


@pytest.mark.parametrize(
    "body",
    [
        {"id": 99, "full_name": "octo/demo", "default_branch": "main"},
        {"id": 42, "full_name": "../../etc", "default_branch": "main"},
        {"id": 42, "full_name": "octo/demo\n", "default_branch": "main"},
        {"id": 42, "full_name": "octo/..", "default_branch": "main"},
        {"id": 42, "full_name": "octo/.", "default_branch": "main"},
    ],
)
@respx.mock
def test_repo_lookup_mismatch_refused(
    rsa_keys: tuple[SecretStr, bytes], body: dict[str, Any]
) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    respx.get(f"{API}/repositories/42").mock(return_value=httpx.Response(200, json=body))
    stale = respx.route(url__startswith=f"{API}/repos/").mock(return_value=httpx.Response(200))
    with pytest.raises(GitHubError, match="different or malformed"):
        run(make_app(rsa_keys).resolve_ref(REPO, "main"))
    assert not stale.called


@respx.mock
def test_can_mint_for_server_error_raises(rsa_keys: tuple[SecretStr, bytes]) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=httpx.Response(500))
    with pytest.raises(GitHubHTTPError) as exc:
        run(make_app(rsa_keys).can_mint_for(7, 42))
    assert exc.value.status == 500


@respx.mock
def test_resolve_ref_default_branch_and_sha(rsa_keys: tuple[SecretStr, bytes]) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    repo = repo_lookup()
    commit = respx.get(f"{API}/repos/octo/demo/commits/trunk").mock(
        return_value=httpx.Response(200, text=SHA)
    )
    assert run(make_app(rsa_keys).resolve_ref(REPO, None)) == SHA
    assert repo.calls.last.request.headers["Authorization"] == f"Bearer {INSTALL_TOKEN}"
    assert commit.calls.last.request.headers["Accept"] == "application/vnd.github.sha"


@pytest.mark.parametrize("ref", ["-bad", "a..b", "x y", "/abs", "a;b"])
def test_resolve_ref_rejects_odd_refs(rsa_keys: tuple[SecretStr, bytes], ref: str) -> None:
    with respx.mock:
        respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
        repo_lookup()
        with pytest.raises(GitHubError, match="invalid ref"):
            run(make_app(rsa_keys).resolve_ref(REPO, ref))


@pytest.mark.parametrize("truncated", [True, False])
@respx.mock
def test_tree_exposes_truncated(rsa_keys: tuple[SecretStr, bytes], truncated: bool) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    repo_lookup()
    route = respx.get(f"{API}/repos/octo/demo/git/trees/{SHA}").mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": SHA,
                "truncated": truncated,
                "tree": [
                    {"path": "a.py", "type": "blob", "size": 10, "mode": "100644"},
                    {"path": "src", "type": "tree", "mode": "040000"},
                ],
            },
        )
    )
    tree = run(make_app(rsa_keys).tree(REPO, SHA))
    assert tree.truncated is truncated
    assert [e.path for e in tree.entries] == ["a.py", "src"]
    assert route.calls.last.request.url.params["recursive"] == "1"


# --- User side -------------------------------------------------------------------------


def form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


@respx.mock
def test_exchange_code_pkce_body() -> None:
    route = respx.post(f"{WEB}/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": USER_TOKEN,
                "expires_in": 28800,
                "refresh_token": REFRESH_TOKEN,
                "refresh_token_expires_in": 15897600,
                "token_type": "bearer",
                "scope": "",
            },
        )
    )
    tokens = run(make_user().exchange_code("the-code", "the-verifier", "http://x/cb"))
    req = route.calls.last.request
    assert req.headers["Accept"] == "application/json"
    assert form(req) == {
        "client_id": "Iv1.client",
        "client_secret": CLIENT_SECRET,
        "code": "the-code",
        "code_verifier": "the-verifier",
        "redirect_uri": "http://x/cb",
    }
    assert "Authorization" not in req.headers
    assert tokens.access_token.get_secret_value() == USER_TOKEN
    assert tokens.refresh_token is not None
    assert tokens.expires_at is not None and tokens.refresh_token_expires_at is not None
    assert USER_TOKEN not in repr(tokens) and REFRESH_TOKEN not in repr(tokens)


@respx.mock
def test_exchange_code_error_is_redacted() -> None:
    respx.post(f"{WEB}/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200, json={"error": "bad_verification_code", "error_description": CLIENT_SECRET}
        )
    )
    with pytest.raises(GitHubAuthError, match="bad_verification_code") as exc:
        run(make_user().exchange_code("c", "v", "http://x/cb"))
    assert CLIENT_SECRET not in str(exc.value)


@respx.mock
def test_refresh_body() -> None:
    route = respx.post(f"{WEB}/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": USER_TOKEN, "refresh_token": "ghr_new", "expires_in": 60}
        )
    )
    tokens = run(make_user().refresh(SecretStr(REFRESH_TOKEN)))
    assert form(route.calls.last.request) == {
        "client_id": "Iv1.client",
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }
    assert isinstance(tokens, UserTokens)


@respx.mock
def test_get_user_and_listing() -> None:
    respx.get(f"{API}/user").mock(
        return_value=httpx.Response(200, json={"id": 5, "login": "octocat", "name": None})
    )
    respx.get(f"{API}/user/installations").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "installations": [
                    {"id": 7, "account": {"login": "octo", "id": 9}, "suspended_at": None}
                ],
            },
        )
    )
    repos = respx.get(f"{API}/user/installations/7/repositories").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "repositories": [
                    {"id": 42, "full_name": "octo/demo", "private": True, "default_branch": "main"}
                ],
            },
        )
    )
    client, tok = make_user(), SecretStr(USER_TOKEN)
    assert run(client.get_user(tok)).id == 5
    assert [i.id for i in run(client.list_installations(tok))] == [7]
    assert [r.id for r in run(client.list_installation_repos(tok, 7))] == [42]
    req = repos.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {USER_TOKEN}"
    assert req.url.params["per_page"] == "100"


@pytest.mark.parametrize(
    ("status", "expected"), [(200, True), (401, False), (403, False), (404, False)]
)
@respx.mock
def test_can_read_repo_by_numeric_id(status: int, expected: bool) -> None:
    route = respx.get(f"{API}/repositories/42").mock(
        return_value=httpx.Response(status, json={"id": 42} if status == 200 else {})
    )
    assert run(make_user().can_read_repo(SecretStr(USER_TOKEN), 42)) is expected
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {USER_TOKEN}"


@respx.mock
def test_can_read_repo_server_error_raises() -> None:
    respx.get(f"{API}/repositories/42").mock(return_value=httpx.Response(502))
    with pytest.raises(GitHubHTTPError):
        run(make_user().can_read_repo(SecretStr(USER_TOKEN), 42))


@respx.mock
def test_revoke_grant_shape() -> None:
    route = respx.delete(f"{API}/applications/Iv1.client/grant").mock(
        return_value=httpx.Response(204)
    )
    run(make_user().revoke_grant(SecretStr(USER_TOKEN)))
    req = route.calls.last.request
    assert req.headers["Authorization"].startswith("Basic ")
    assert json.loads(req.content) == {"access_token": USER_TOKEN}


# --- Rate limits -----------------------------------------------------------------------


@respx.mock
def test_retry_after_is_honoured() -> None:
    sleeps = Sleeps()
    route = respx.get(f"{API}/repositories/42").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(403, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"id": 42}),
        ]
    )
    user = HttpGitHubUser(
        GitHubHTTP(sleep=sleeps), client_id="c", client_secret=SecretStr(CLIENT_SECRET)
    )
    assert run(user.can_read_repo(SecretStr(USER_TOKEN), 42)) is True
    assert sleeps.calls == [3.0, 1.0]
    assert route.call_count == 3


@respx.mock
def test_rate_limit_reset_is_honoured() -> None:
    sleeps = Sleeps()
    respx.get(f"{API}/user").mock(
        side_effect=[
            httpx.Response(
                403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"}
            ),
            httpx.Response(200, json={"id": 1, "login": "a"}),
        ]
    )
    http = GitHubHTTP(sleep=sleeps, clock=lambda: 1000.0)
    user = HttpGitHubUser(http, client_id="c", client_secret=SecretStr(CLIENT_SECRET))
    assert run(user.get_user(SecretStr(USER_TOKEN))).login == "a"
    assert sleeps.calls == [11.0]


@respx.mock
def test_rate_limit_is_bounded() -> None:
    sleeps = Sleeps()
    respx.get(f"{API}/user").mock(return_value=httpx.Response(429, headers={"Retry-After": "5"}))
    http = GitHubHTTP(sleep=sleeps, max_retries=2)
    user = HttpGitHubUser(http, client_id="c", client_secret=SecretStr(CLIENT_SECRET))
    with pytest.raises(GitHubRateLimitedError) as exc:
        run(user.get_user(SecretStr(USER_TOKEN)))
    assert sleeps.calls == [5.0, 5.0]
    assert exc.value.retry_after == 5.0


@respx.mock
def test_rate_limit_wait_over_cap_raises_without_sleeping() -> None:
    sleeps = Sleeps()
    respx.get(f"{API}/user").mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    http = GitHubHTTP(sleep=sleeps, max_wait_s=60)
    user = HttpGitHubUser(http, client_id="c", client_secret=SecretStr(CLIENT_SECRET))
    with pytest.raises(GitHubRateLimitedError):
        run(user.get_user(SecretStr(USER_TOKEN)))
    assert sleeps.calls == []


def test_client_has_timeout() -> None:
    http = GitHubHTTP()
    assert http._client.timeout.read == 10.0
    run(http.aclose())


# --- No token in error text ------------------------------------------------------------


def _all_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc)


@respx.mock
def test_transport_error_hides_token() -> None:
    respx.get(f"{API}/user").mock(
        side_effect=httpx.ConnectError(f"boom Authorization: Bearer {USER_TOKEN} {USER_TOKEN}")
    )
    with pytest.raises(GitHubHTTPError) as exc:
        run(make_user().get_user(SecretStr(USER_TOKEN)))
    text = _all_text(exc.value)
    assert USER_TOKEN not in text
    assert "ghu_USER" not in text
    assert exc.value.__cause__ is None


@respx.mock
def test_error_body_echoing_token_is_redacted() -> None:
    respx.get(f"{API}/user").mock(
        return_value=httpx.Response(500, json={"message": f"oops {USER_TOKEN} ghp_other123"})
    )
    with pytest.raises(GitHubHTTPError) as exc:
        run(make_user().get_user(SecretStr(USER_TOKEN)))
    text = _all_text(exc.value)
    assert USER_TOKEN not in text and "ghp_other123" not in text
    assert exc.value.status == 500


@respx.mock
def test_install_token_error_hides_jwt(rsa_keys: tuple[SecretStr, bytes]) -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    with pytest.raises(GitHubHTTPError) as exc:
        run(make_app(rsa_keys).installation_token(7, 42))
    assert "eyJ" not in _all_text(exc.value)


def test_clients_repr_hide_secrets(rsa_keys: tuple[SecretStr, bytes]) -> None:
    assert "PRIVATE" not in repr(make_app(rsa_keys))
    assert CLIENT_SECRET not in repr(make_user())


# --- Real fetch_snapshot (token via respx, git against a local bare repo) --------------


@respx.mock
def test_real_fetch_snapshot_keeps_token_out(
    rsa_keys: tuple[SecretStr, bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = fixture_bare_repos()["fixture/demo"]
    git_root = bare.parent.parent  # <root>/fixture/demo.git
    respx.post(f"{API}/app/installations/7/access_tokens").mock(return_value=token_ok())
    repo_lookup("fixture/demo", clone_url="https://github.com/fixture/demo.git")
    # An inherited GIT_* variable must not reach git (scrubbed env).
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    app = make_app(rsa_keys, git_url=git_root.as_uri())
    sha = _head(bare)
    dest = tmp_path / "snap"
    run(app.fetch_snapshot(REPO, sha, dest, 20))
    try:
        assert (dest / "README.md").is_file()
        for path in dest.rglob("*"):
            if path.is_file():
                assert INSTALL_TOKEN.encode() not in path.read_bytes(), path
    finally:
        remove_snapshot(dest)


def _head(bare: Path) -> str:
    import subprocess

    out = subprocess.run(  # noqa: S603
        ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_error_message_redacted_before_truncation() -> None:
    # The secret straddles the 200-char cut; a prefix must not survive.
    req = httpx.Request("POST", f"{WEB}/login/oauth/access_token")
    resp = httpx.Response(500, json={"message": "x" * 190 + CLIENT_SECRET}, request=req)
    text = str(error_for(resp, CLIENT_SECRET))
    assert CLIENT_SECRET[:8] not in text


def test_git_stderr_redacted_before_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    # The token straddles the 500-char tail cut; its suffix must not survive.
    suffix = "Q" * 30
    token = "ghs_" + "P" * 10 + suffix

    class FakeProc:
        returncode = 1

        async def communicate(self, _stdin: bytes | None) -> tuple[bytes, bytes]:
            return b"", ("x" * 600 + token + "y" * (500 - len(suffix))).encode()

    async def fake_exec(*_a: Any, **_k: Any) -> FakeProc:
        return FakeProc()

    monkeypatch.setattr(app_client.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(SnapshotError) as exc:
        run(app_client._git(["fetch"], cwd=Path("."), env={}, token=SecretStr(token)))
    assert suffix[:8] not in str(exc.value)

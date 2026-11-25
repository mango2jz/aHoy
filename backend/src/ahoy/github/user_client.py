"""The GitHub user side: OAuth web flow (PKCE), refresh, identity, installs, repo access.

User tokens are passed in as `SecretStr` and only unwrapped into the `Authorization`
header of a single request. `can_read_repo` checks by numeric id (`GET /repositories/{id}`)
so renames and transfers cannot confuse it; 401/403/404 all mean "no access".
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, SecretStr

from ahoy.github.http import (
    API_URL,
    WEB_URL,
    GitHubAuthError,
    GitHubHTTP,
    basic,
    bearer,
    error_for,
    redact,
)

_PER_PAGE = 100
_MAX_PAGES = 50


class UserTokens(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None


class GitHubUserInfo(BaseModel):
    id: int
    login: str
    name: str | None = None
    avatar_url: str | None = None


class Installation(BaseModel):
    id: int
    account_login: str
    account_id: int
    suspended: bool = False


class Repo(BaseModel):
    id: int
    full_name: str
    private: bool
    default_branch: str = "main"


class GitHubUser(Protocol):
    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> UserTokens:
        """Exchange an OAuth `code` plus the PKCE verifier for user tokens."""

    async def refresh(self, refresh_token: SecretStr) -> UserTokens:
        """Trade a (single-use) refresh token for new access and refresh tokens."""

    async def get_user(self, token: SecretStr) -> GitHubUserInfo:
        """The signed-in user's numeric id and login."""

    async def list_installations(self, token: SecretStr) -> list[Installation]:
        """App installations this user can access."""

    async def list_installation_repos(self, token: SecretStr, installation_id: int) -> list[Repo]:
        """Repos of one installation that this user can access."""

    async def can_read_repo(self, token: SecretStr, repo_id: int) -> bool:
        """True iff `GET /repositories/{repo_id}` with this user's token returns 200."""

    async def revoke_grant(self, token: SecretStr) -> None:
        """Revoke the user's authorization of the App (all their tokens)."""


def _expiry(seconds: Any, now: float) -> datetime | None:
    if seconds in (None, ""):
        return None
    return datetime.fromtimestamp(now, UTC) + timedelta(seconds=int(seconds))


class HttpGitHubUser:
    """The real user-token client."""

    def __init__(
        self,
        http: GitHubHTTP,
        *,
        client_id: str,
        client_secret: SecretStr,
        api_url: str = API_URL,
        web_url: str = WEB_URL,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._api = api_url.rstrip("/")
        self._web = web_url.rstrip("/")

    def __repr__(self) -> str:
        return f"HttpGitHubUser(client_id={self._client_id!r})"

    async def _token_request(self, form: dict[str, str], *secrets: SecretStr | str) -> UserTokens:
        body = {
            "client_id": self._client_id,
            "client_secret": self._client_secret.get_secret_value(),
            **form,
        }
        now = time.time()
        resp = await self._http.request(
            "POST",
            f"{self._web}/login/oauth/access_token",
            accept="application/json",
            data=body,
            secrets=(self._client_secret, *secrets),
        )
        if resp.status_code != 200:
            raise error_for(resp, self._client_secret, *secrets, what="token request")
        payload = resp.json()
        if "error" in payload or not payload.get("access_token"):
            # GitHub reports OAuth errors as 200 + {"error": ...}.
            code = redact(str(payload.get("error", "no access_token")), *secrets)
            raise GitHubAuthError(f"GitHub refused the token request: {code[:80]}")
        refresh = payload.get("refresh_token")
        return UserTokens(
            access_token=SecretStr(payload["access_token"]),
            refresh_token=SecretStr(refresh) if refresh else None,
            expires_at=_expiry(payload.get("expires_in"), now),
            refresh_token_expires_at=_expiry(payload.get("refresh_token_expires_in"), now),
        )

    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> UserTokens:
        return await self._token_request(
            {"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri},
            code,
            code_verifier,
        )

    async def refresh(self, refresh_token: SecretStr) -> UserTokens:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()},
            refresh_token.get_secret_value(),
        )

    async def _get(
        self, token: SecretStr, path: str, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        return await self._http.request(
            "GET", f"{self._api}{path}", auth=SecretStr(bearer(token)), params=params
        )

    async def get_user(self, token: SecretStr) -> GitHubUserInfo:
        resp = await self._get(token, "/user")
        if resp.status_code != 200:
            raise error_for(resp, token, what="get user")
        return GitHubUserInfo.model_validate(resp.json())

    async def _paged(self, token: SecretStr, path: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            resp = await self._get(token, path, {"per_page": _PER_PAGE, "page": page})
            if resp.status_code != 200:
                raise error_for(resp, token, what=f"list {key}")
            chunk = resp.json().get(key, [])
            items.extend(chunk)
            if len(chunk) < _PER_PAGE:
                break
        return items

    async def list_installations(self, token: SecretStr) -> list[Installation]:
        raw = await self._paged(token, "/user/installations", "installations")
        return [
            Installation(
                id=i["id"],
                account_login=i["account"]["login"],
                account_id=i["account"]["id"],
                suspended=i.get("suspended_at") is not None,
            )
            for i in raw
        ]

    async def list_installation_repos(self, token: SecretStr, installation_id: int) -> list[Repo]:
        raw = await self._paged(
            token, f"/user/installations/{int(installation_id)}/repositories", "repositories"
        )
        return [Repo.model_validate(r) for r in raw]

    async def can_read_repo(self, token: SecretStr, repo_id: int) -> bool:
        resp = await self._get(token, f"/repositories/{int(repo_id)}")
        if resp.status_code == 200:
            return True
        if resp.status_code in (401, 403, 404):
            return False
        raise error_for(resp, token, what="check repo access")

    async def revoke_grant(self, token: SecretStr) -> None:
        resp = await self._http.request(
            "DELETE",
            f"{self._api}/applications/{self._client_id}/grant",
            auth=SecretStr(basic(self._client_id, self._client_secret)),
            json={"access_token": token.get_secret_value()},
            secrets=(token, self._client_secret),
        )
        # 404/422: the grant or token is already gone, which is the goal.
        if resp.status_code not in (204, 404, 422):
            raise error_for(resp, token, self._client_secret, what="revoke grant")

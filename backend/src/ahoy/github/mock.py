"""An in-process fake GitHub for dev and tests (`--github mock`). Implements both Protocols.

Users alice, bob and mallory. One installation (`fixture`) holds `fixture/demo` (private;
alice and bob have access) and `fixture/public` (public). Both repos are built once per
process from the vendored fixture via `evals/make_fixture_git.py` into a temp dir, as
bare repos. `fetch_snapshot` runs the real snapshot code path against them, so the
denylist stripping is exercised. Tokens look like GitHub's (`ghu_…`, `ghr_…`, `ghs_…`)
so the secret scans are meaningful. Refuses to construct unless AHOY_ENV is dev or test.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from ahoy.github.app_client import (
    RepoTarget,
    Tree,
    TreeEntry,
    _git_env,
    check_ref,
    check_sha,
    fetch_snapshot_from,
)
from ahoy.github.http import GitHubAuthError, GitHubError, GitHubHTTPError
from ahoy.github.user_client import GitHubUserInfo, Installation, Repo, UserTokens

MOCK_ENVS = ("dev", "test")
INSTALLATION_ID = 5001
INSTALLATION_ACCOUNT = ("fixture", 9001)

USERS: dict[str, int] = {"alice": 1001, "bob": 1002, "mallory": 1003}


@dataclass(frozen=True)
class MockRepo:
    id: int
    full_name: str  # the original name; `MockGitHub.name_of` gives the current one
    private: bool
    members: frozenset[str]


REPOS: tuple[MockRepo, ...] = (
    MockRepo(7001, "fixture/demo", private=True, members=frozenset({"alice", "bob"})),
    MockRepo(7002, "fixture/public", private=False, members=frozenset({"alice", "bob"})),
)

_MAKE_FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "make_fixture_git.py"
_build_lock = threading.Lock()
_built: dict[str, Path] = {}


def fixture_bare_repos() -> dict[str, Path]:
    """Build (once per process) and return `{full_name: bare repo path}`."""
    with _build_lock:
        if _built:
            return dict(_built)
        if not _MAKE_FIXTURE.is_file():
            raise GitHubError(f"fixture builder not found: {_MAKE_FIXTURE}")
        root = Path(tempfile.mkdtemp(prefix="ahoy-mock-github-"))
        atexit.register(shutil.rmtree, root, True)
        work = root / "work"
        env = _git_env(str(root))
        subprocess.run(  # noqa: S603
            [sys.executable, "-I", str(_MAKE_FIXTURE), str(work)],
            check=True,
            capture_output=True,
            env=env,
        )
        git = shutil.which("git") or "git"
        for repo in REPOS:
            bare = root / f"{repo.full_name}.git"
            bare.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(  # noqa: S603
                [git, "clone", "-q", "--bare", "--no-local", str(work), str(bare)],
                check=True,
                capture_output=True,
                env=env,
            )
            _built[repo.full_name] = bare
        return dict(_built)


def _token(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(36))


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _not_found(what: str) -> GitHubHTTPError:
    return GitHubHTTPError(f"mock GitHub: {what} not found", status=404)


@dataclass
class _Grant:
    user: str
    code_challenge: str | None


@dataclass
class _State:
    codes: dict[str, _Grant] = field(default_factory=dict)
    access: dict[str, str] = field(default_factory=dict)  # access token -> user
    refresh: dict[str, str] = field(default_factory=dict)  # refresh token -> user
    revoked: set[tuple[str, int]] = field(default_factory=set)
    removed: set[int] = field(default_factory=set)
    truncated: set[int] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)  # repo id -> current name


class MockGitHub:
    """Fake GitHub implementing `GitHubApp` and `GitHubUser`, with test hooks."""

    def __init__(self, *, ahoy_env: str, token_lifetime_s: int = 8 * 3600) -> None:
        env_var = os.environ.get("AHOY_ENV")
        if ahoy_env not in MOCK_ENVS or (env_var is not None and env_var not in MOCK_ENVS):
            raise RuntimeError("mock GitHub is only allowed when AHOY_ENV is dev or test")
        self._token_lifetime_s = token_lifetime_s
        self._state = _State()
        self.users = dict(USERS)
        self.repos = {r.id: r for r in REPOS}

    def __repr__(self) -> str:
        return "MockGitHub()"

    # --- test hooks -------------------------------------------------------------------

    def revoke_access(self, user: str, repo: str | int) -> None:
        """`user` can no longer read `repo` (even if public) until `restore_access`."""
        self._state.revoked.add((user, self._repo(repo).id))

    def restore_access(self, user: str, repo: str | int) -> None:
        self._state.revoked.discard((user, self._repo(repo).id))

    def remove_from_installation(self, repo: str | int) -> None:
        """The App can no longer mint for `repo`; private repos also vanish for users."""
        self._state.removed.add(self._repo(repo).id)

    def rename_repo(self, repo: str | int, new_full_name: str) -> None:
        """Rename (or transfer) `repo`. Its id, and so its sessions, stay the same."""
        mock = self._repo(repo)
        for other in REPOS:
            if other.id != mock.id and self.name_of(other) == new_full_name:
                raise ValueError(f"{new_full_name} is taken by repo {other.id}")
        self._state.names[mock.id] = new_full_name

    def name_of(self, repo: MockRepo) -> str:
        """The repo's current name (`MockRepo.full_name` is the original one)."""
        return self._state.names.get(repo.id, repo.full_name)

    def set_tree_truncated(self, repo: str | int, truncated: bool) -> None:
        """Make `tree()` report GitHub's `truncated: true` for `repo` (too large to list)."""
        rid = self._repo(repo).id
        if truncated:
            self._state.truncated.add(rid)
        else:
            self._state.truncated.discard(rid)

    def add_to_installation(self, repo: str | int) -> None:
        self._state.removed.discard(self._repo(repo).id)

    def issue_code(self, user: str, code_challenge: str | None = None) -> str:
        """What GitHub's authorize page would do: a one-time code bound to the challenge."""
        if user not in self.users:
            raise KeyError(user)
        code = secrets.token_hex(10)
        self._state.codes[code] = _Grant(user, code_challenge)
        return code

    def bare_repo(self, repo: str | int) -> Path:
        return fixture_bare_repos()[self._repo(repo).full_name]

    # --- helpers ----------------------------------------------------------------------

    def _repo(self, repo: str | int) -> MockRepo:
        for r in REPOS:
            if repo in (r.id, self.name_of(r)):
                return r
        raise KeyError(repo)

    def _user_for(self, token: SecretStr) -> str:
        user = self._state.access.get(token.get_secret_value())
        if user is None:
            raise GitHubHTTPError("mock GitHub: bad credentials", status=401)
        return user

    def _user_can_read(self, user: str, repo: MockRepo) -> bool:
        if (user, repo.id) in self._state.revoked:
            return False
        if repo.private:
            # A GitHub App user token sees private repos only where the App is installed.
            return user in repo.members and repo.id not in self._state.removed
        return True

    def _issue_tokens(self, user: str) -> UserTokens:
        access, refresh = _token("ghu_"), _token("ghr_")
        self._state.access[access] = user
        self._state.refresh[refresh] = user
        now = datetime.now(UTC)
        return UserTokens(
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh),
            expires_at=now + timedelta(seconds=self._token_lifetime_s),
            refresh_token_expires_at=now + timedelta(days=180),
        )

    def _target(self, repo: RepoTarget) -> MockRepo:
        try:
            mock = self._repo(repo.id)
        except KeyError:
            raise _not_found("repository") from None
        if repo.installation_id != INSTALLATION_ID or mock.id in self._state.removed:
            raise GitHubHTTPError("mock GitHub: repository not in installation", status=422)
        return mock

    def _bare_git(self, repo: MockRepo, *args: str) -> str:
        bare = fixture_bare_repos()[repo.full_name]
        git = shutil.which("git") or "git"
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.run(  # noqa: S603
                [git, *args], cwd=bare, env=_git_env(home), capture_output=True, text=True
            )
        if proc.returncode != 0:
            raise _not_found("ref")
        return proc.stdout

    # --- GitHubUser -------------------------------------------------------------------

    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> UserTokens:
        grant = self._state.codes.pop(code, None)
        if grant is None:
            raise GitHubAuthError("GitHub refused the token request: bad_verification_code")
        if grant.code_challenge is not None and _s256(code_verifier) != grant.code_challenge:
            raise GitHubAuthError("GitHub refused the token request: invalid_grant")
        return self._issue_tokens(grant.user)

    async def refresh(self, refresh_token: SecretStr) -> UserTokens:
        user = self._state.refresh.pop(refresh_token.get_secret_value(), None)  # single use
        if user is None:
            raise GitHubAuthError("GitHub refused the token request: bad_refresh_token")
        return self._issue_tokens(user)

    async def get_user(self, token: SecretStr) -> GitHubUserInfo:
        user = self._user_for(token)
        return GitHubUserInfo(id=self.users[user], login=user, name=user.title())

    async def list_installations(self, token: SecretStr) -> list[Installation]:
        user = self._user_for(token)
        visible = [r for r in REPOS if r.id not in self._state.removed]
        if not any(user in r.members and (user, r.id) not in self._state.revoked for r in visible):
            return []
        login, account_id = INSTALLATION_ACCOUNT
        return [Installation(id=INSTALLATION_ID, account_login=login, account_id=account_id)]

    async def list_installation_repos(self, token: SecretStr, installation_id: int) -> list[Repo]:
        user = self._user_for(token)
        if installation_id != INSTALLATION_ID:
            raise _not_found("installation")
        return [
            Repo(id=r.id, full_name=self.name_of(r), private=r.private, default_branch="main")
            for r in REPOS
            if r.id not in self._state.removed
            and user in r.members
            and (user, r.id) not in self._state.revoked
        ]

    async def can_read_repo(self, token: SecretStr, repo_id: int) -> bool:
        try:
            user = self._user_for(token)
            repo = self._repo(repo_id)
        except (GitHubHTTPError, KeyError):
            return False
        return self._user_can_read(user, repo)

    async def revoke_grant(self, token: SecretStr) -> None:
        user = self._state.access.get(token.get_secret_value())
        if user is None:
            return
        for table in (self._state.access, self._state.refresh):
            for tok in [t for t, u in table.items() if u == user]:
                del table[tok]

    # --- GitHubApp --------------------------------------------------------------------

    async def can_mint_for(self, installation_id: int, repo_id: int) -> bool:
        return (
            installation_id == INSTALLATION_ID
            and repo_id in self.repos
            and repo_id not in self._state.removed
        )

    async def resolve_ref(self, repo: RepoTarget, ref: str | None) -> str:
        mock = self._target(repo)
        rev = check_ref(ref) if ref else "HEAD"
        sha = self._bare_git(mock, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
        return check_sha(sha.strip())

    async def tree(self, repo: RepoTarget, sha: str) -> Tree:
        mock = self._target(repo)
        out = self._bare_git(mock, "ls-tree", "-r", "-t", "-l", "-z", check_sha(sha))
        entries: list[TreeEntry] = []
        for record in filter(None, out.split("\0")):
            meta, path = record.split("\t", 1)
            mode, kind, _obj, size = meta.split()
            entries.append(
                TreeEntry(
                    path=path,
                    type=kind,
                    size=int(size) if size != "-" else None,
                    mode=mode,
                )
            )
        return Tree(sha=sha, truncated=mock.id in self._state.truncated, entries=entries)

    async def fetch_snapshot(self, repo: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        mock = self._target(repo)
        bare = fixture_bare_repos()[mock.full_name]
        # A fake installation token rides along exactly like the real one, so a token
        # leak in the snapshot code path would show up in the mock's tests too.
        await fetch_snapshot_from(bare.as_uri(), sha, dest, depth, SecretStr(_token("ghs_")))

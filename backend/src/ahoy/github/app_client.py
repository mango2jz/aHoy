"""The GitHub App side: App JWT, narrowed installation tokens, ref/tree lookups, snapshots.

Installation tokens are minted on demand, narrowed to one repository and
`contents: read`, and never stored. `fetch_snapshot_from()` is the one snapshot code
path; the mock GitHub runs it too, against a local bare repo.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote

import jwt
from pydantic import BaseModel, SecretStr

from ahoy.github.http import (
    API_URL,
    WEB_URL,
    GitHubError,
    GitHubHTTP,
    GitHubRateLimitedError,
    basic,
    bearer,
    error_for,
    redact,
)
from ahoy.sandbox.paths import is_denylisted

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
GIT_TIMEOUT_S = 300.0


class RepoTarget(BaseModel):
    """A repo the App acts on: the numeric id (its identity) and its installation.

    There is deliberately no name: repos are renamed and transferred, and a stored name
    can come to mean a different repo. The current name is looked up by id on each use.
    """

    id: int
    installation_id: int


class RepoInfo(BaseModel):
    """GitHub's current answer for a repo id."""

    id: int
    full_name: str
    default_branch: str
    clone_url: str | None = None


class TreeEntry(BaseModel):
    path: str
    type: Literal["blob", "tree", "commit"]
    size: int | None = None
    mode: str = ""


class Tree(BaseModel):
    sha: str
    truncated: bool
    entries: list[TreeEntry]


class SnapshotError(GitHubError):
    """Fetching or preparing a snapshot failed. The message is redacted."""


class GitHubApp(Protocol):
    async def can_mint_for(self, installation_id: int, repo_id: int) -> bool:
        """True if the installation can still mint a token for this repo."""

    async def resolve_ref(self, repo: RepoTarget, ref: str | None) -> str:
        """Full commit SHA for `ref`; the default branch when `ref` is empty."""

    async def tree(self, repo: RepoTarget, sha: str) -> Tree:
        """The recursive tree at `sha`, with GitHub's `truncated` flag."""

    async def fetch_snapshot(self, repo: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        """Fetch `sha` into `dest`, strip denylisted files, make it read-only."""


def check_ref(ref: str) -> str:
    if not _REF_RE.match(ref) or ref.startswith(("-", "/")) or ".." in ref or len(ref) > 255:
        raise GitHubError(f"invalid ref: {ref[:80]!r}")
    return ref


def check_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise GitHubError("not a full commit SHA")
    return sha


def make_app_jwt(app_id: str, private_key: SecretStr, *, now: float | None = None) -> SecretStr:
    """RS256 App JWT: `iat` 60 s in the past (clock drift), `exp` 9 min ahead (max is 10)."""
    issued = int(now if now is not None else time.time())
    claims = {"iat": issued - 60, "exp": issued + 540, "iss": app_id}
    return SecretStr(jwt.encode(claims, private_key.get_secret_value(), algorithm="RS256"))


class HttpGitHubApp:
    """The real GitHub App client."""

    def __init__(
        self,
        http: GitHubHTTP,
        *,
        app_id: str,
        private_key: SecretStr,
        api_url: str = API_URL,
        git_url: str = WEB_URL,
    ) -> None:
        self._http = http
        self._app_id = app_id
        self._private_key = private_key
        self._api = api_url.rstrip("/")
        self._git = git_url.rstrip("/")

    def __repr__(self) -> str:
        return f"HttpGitHubApp(app_id={self._app_id!r})"

    async def installation_token(self, installation_id: int, repo_id: int) -> SecretStr:
        """Mint a 1 h token for exactly one repo with `contents: read`. Never stored."""
        app_jwt = make_app_jwt(self._app_id, self._private_key)
        resp = await self._http.request(
            "POST",
            f"{self._api}/app/installations/{int(installation_id)}/access_tokens",
            auth=SecretStr(bearer(app_jwt)),
            json={"repository_ids": [int(repo_id)], "permissions": {"contents": "read"}},
        )
        if resp.status_code != 201:
            raise error_for(resp, app_jwt, what="mint installation token")
        body = resp.json()
        token = SecretStr(str(body.get("token", "")))
        perms = body.get("permissions") or {}
        repos = body.get("repositories")
        repo_ids = (
            [r.get("id") if isinstance(r, dict) else None for r in repos]
            if isinstance(repos, list)
            else None
        )
        # Defense in depth: refuse a token broader than we asked for. A missing
        # `repositories` list means the token is not narrowed to one repo: fail closed.
        if (
            not token.get_secret_value()
            or not isinstance(perms, dict)
            or any(v != "read" for v in perms.values())
            or set(perms) - {"contents", "metadata"}
            or repo_ids != [int(repo_id)]
        ):
            raise GitHubError("installation token is broader than requested; refused")
        return token

    async def can_mint_for(self, installation_id: int, repo_id: int) -> bool:
        try:
            await self.installation_token(installation_id, repo_id)
        except GitHubRateLimitedError:
            raise  # rate limiting must never read as "no access"
        except GitHubError as exc:
            if getattr(exc, "status", None) in (401, 403, 404, 422):
                return False
            raise
        return True

    async def repo_info(self, repo_id: int, token: SecretStr) -> RepoInfo:
        """The repo's current name, default branch and clone URL, looked up by id."""
        resp = await self._http.request(
            "GET", f"{self._api}/repositories/{int(repo_id)}", auth=SecretStr(bearer(token))
        )
        if resp.status_code != 200:
            raise error_for(resp, token, what="look up repo")
        info = RepoInfo.model_validate(resp.json())
        # fullmatch: `$` would accept a trailing newline. `.`/`..` names would be dot
        # segments that httpx collapses into another API path.
        if (
            info.id != int(repo_id)
            or not _NAME_RE.fullmatch(info.full_name)
            or info.full_name.partition("/")[2] in (".", "..")
        ):
            raise GitHubError("GitHub returned a different or malformed repo for this id")
        return info

    def _repo_url(self, info: RepoInfo) -> str:
        owner, _, name = info.full_name.partition("/")
        return f"{self._api}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    async def _open(self, repo: RepoTarget) -> tuple[SecretStr, RepoInfo]:
        token = await self.installation_token(repo.installation_id, repo.id)
        return token, await self.repo_info(repo.id, token)

    async def resolve_ref(self, repo: RepoTarget, ref: str | None) -> str:
        token, info = await self._open(repo)
        target = quote(check_ref(ref or info.default_branch), safe="/")
        resp = await self._http.request(
            "GET",
            f"{self._repo_url(info)}/commits/{target}",
            auth=SecretStr(bearer(token)),
            accept="application/vnd.github.sha",
        )
        if resp.status_code != 200:
            raise error_for(resp, token, what="resolve ref")
        return check_sha(resp.text.strip())

    async def tree(self, repo: RepoTarget, sha: str) -> Tree:
        token, info = await self._open(repo)
        resp = await self._http.request(
            "GET",
            f"{self._repo_url(info)}/git/trees/{check_sha(sha)}",
            auth=SecretStr(bearer(token)),
            params={"recursive": "1"},
        )
        if resp.status_code != 200:
            raise error_for(resp, token, what="read tree")
        body = resp.json()
        return Tree(
            sha=body["sha"],
            truncated=bool(body.get("truncated", False)),
            entries=[TreeEntry.model_validate(e) for e in body.get("tree", [])],
        )

    async def fetch_snapshot(self, repo: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        token, info = await self._open(repo)
        built = f"{self._git}/{info.full_name}.git"
        # Prefer GitHub's own clone URL, but only on the git host we are configured for.
        url = info.clone_url if info.clone_url == built else built
        await fetch_snapshot_from(url, sha, dest, depth, token)


# --- snapshot -------------------------------------------------------------------------


def git_auth_header(token: SecretStr) -> str:
    """The `Authorization` value for git over HTTPS: Basic `x-access-token:<token>`, the
    form GitHub documents for installation tokens (as actions/checkout uses)."""
    return basic("x-access-token", token)


def _git_env(home: str) -> dict[str, str]:
    """A scrubbed environment: nothing inherited (no GIT_*, no credentials), empty HOME."""
    git = shutil.which("git")
    path_dirs = ["/usr/bin", "/bin"]
    if git:
        path_dirs.insert(0, str(Path(git).parent))
    return {
        "PATH": os.pathsep.join(path_dirs),
        "HOME": home,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


# Applied to every git process: no credential helpers (Apple git ships osxkeychain), no
# hooks, no fsmonitor, no submodule recursion.
_GIT_SAFE = (
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
)


async def _git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    token: SecretStr | None = None,
    stdin: bytes | None = None,
) -> str:
    git = shutil.which("git")
    if git is None:
        raise SnapshotError("git is not installed")
    argv = [git, *_GIT_SAFE]
    if token is not None:
        # The only place the token appears: this one process's argv.
        argv += ["-c", f"http.extraHeader=Authorization: {git_auth_header(token)}"]
    argv += args
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), GIT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SnapshotError(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        secrets = (token, git_auth_header(token)) if token is not None else ()
        # Redact before truncating: a cut through the token would defeat verbatim matching.
        detail = redact(err.decode(errors="replace").strip(), *secrets)[-500:]
        raise SnapshotError(f"git {args[0]} failed ({proc.returncode}): {detail}")
    return out.decode(errors="replace")


def _strip_denylisted(root: Path) -> list[str]:
    """Delete every working-tree path `is_denylisted` matches. Returns the relative paths."""
    removed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in list(dirnames):
            rel = (rel_dir / name).as_posix()
            if is_denylisted(rel):
                full = Path(dirpath) / name
                if full.is_symlink():
                    full.unlink()
                else:
                    shutil.rmtree(full)
                dirnames.remove(name)
                removed.append(rel)
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if is_denylisted(rel):
                (Path(dirpath) / name).unlink()
                removed.append(rel)
    return removed


def _make_read_only(root: Path) -> None:
    """`chmod -R a-w`, without following symlinks."""
    write = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in [*filenames, *dirnames]:
            full = Path(dirpath) / name
            if not full.is_symlink():
                full.chmod(full.stat().st_mode & ~write)
    root.chmod(root.stat().st_mode & ~write)


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            full = Path(dirpath) / name
            if not full.is_symlink():
                full.chmod(full.stat().st_mode | stat.S_IWUSR)
    root.chmod(root.stat().st_mode | stat.S_IWUSR)


def remove_snapshot(dest: Path) -> None:
    """Delete a (read-only) snapshot directory."""
    if dest.exists():
        _make_writable(dest)
        shutil.rmtree(dest)


async def fetch_snapshot_from(
    url: str, sha: str, dest: Path, depth: int, token: SecretStr | None
) -> None:
    """The snapshot code path, shared by the real App client and the mock.

    `git init`, shallow-fetch exactly `sha` (token only on that process's argv, scrubbed
    env), detached checkout, delete denylisted paths and mark them skip-worktree (so
    status and diff stay clean), then `chmod -R a-w`. On failure `dest` is removed.
    """
    check_sha(sha)
    if depth < 1:
        raise SnapshotError("depth must be at least 1")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SnapshotError("snapshot destination is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="ahoy-git-home-") as home:
            env = _git_env(home)
            await _git(["init", "-q", "--template=", "."], cwd=dest, env=env)
            await _git(
                [
                    "fetch",
                    "-q",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--no-write-fetch-head",
                    f"--depth={int(depth)}",
                    "--",
                    url,
                    sha,
                ],
                cwd=dest,
                env=env,
                token=token,
            )
            await _git(["checkout", "-q", "--detach", sha], cwd=dest, env=env)
            head = (await _git(["rev-parse", "HEAD"], cwd=dest, env=env)).strip()
            if head != sha:
                raise SnapshotError("checked-out commit does not match the requested SHA")
            removed = await asyncio.to_thread(_strip_denylisted, dest)
            tracked = (await _git(["ls-files", "-z"], cwd=dest, env=env)).split("\0")
            hidden = [p for p in tracked if p and is_denylisted(p)]
            if hidden:
                await _git(
                    ["update-index", "--skip-worktree", "-z", "--stdin"],
                    cwd=dest,
                    env=env,
                    stdin=b"".join(p.encode() + b"\0" for p in hidden),
                )
            leftover = [p for p in hidden if (dest / p).exists() or (dest / p).is_symlink()]
            if leftover or (hidden and not removed):
                raise SnapshotError("denylisted files survived stripping")
            await asyncio.to_thread(_make_read_only, dest)
    except BaseException:
        await asyncio.to_thread(remove_snapshot, dest)
        raise

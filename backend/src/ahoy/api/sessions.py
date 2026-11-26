"""`/api/sessions`: create, list and read sessions (SPEC -> GitHub integration -> Creating a
session, steps 1, 2 and 4-7; the session limit, step 3, is T28; starting the Overview is T7).

Creation answers quickly: access check, private-repo notice, ref -> SHA, tree size check,
then the `preparing` row (201). A background task (`Preparer`) leases the snapshot through
the repo cache, checks its on-disk size and sets `ready` or `failed` with a short, fixed
reason (never exception text, which could carry a URL or token).

Refs: GitHub resolves commits that exist only in a fork of the repo (pasted SHAs, and refs
like `refs/pull/N/head`). So a ref is accepted as is only when it is the default branch or
exactly the name of a branch or tag of the repo; any other ref GitHub resolves must be
reachable from the default branch (`compare default...sha` is "identical" or "behind"),
else 422 `ref_not_on_default_branch`.

Visibility and the default branch come from GitHub by repo id at creation time (the 60 s
listing only supplies the installation), so a repo made private a moment ago needs the
notice.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ahoy.api.repos import repo_list
from ahoy.auth.deps import CurrentUser
from ahoy.auth.sessions import DB, Auth, AuthState
from ahoy.authz import Action, check_repo_access, has_repo_access, load_session
from ahoy.github.app_client import GitHubApp, RepoTarget, SnapshotError, Tree, check_ref
from ahoy.github.http import GitHubError, GitHubHTTPError, GitHubRateLimitedError
from ahoy.github.repo_cache import Fetch, RepoCache
from ahoy.harness.runner import StepRunner
from ahoy.harness.schemas import GraphNode, Session, SessionStatus, StepStatus, User
from ahoy.store import auth as auth_store
from ahoy.store import repo

log = logging.getLogger("ahoy.sessions")

MAX_SOURCE_FILES = 500
MAX_TREE_BYTES = 200 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
INTERRUPTED = "interrupted"

# A path under any of these directories (at any depth) is vendored, not source.
VENDORED_DIRS = frozenset({"node_modules", "vendor", "third_party", "dist", "build", ".git"})
# Binary or minified by extension: images, fonts, archives, media, compiled objects.
BINARY_EXTENSIONS = frozenset(
    {
        # images
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
        ".psd", ".svg", ".avif", ".heic",
        # fonts
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        # archives
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
        ".whl", ".zst",
        # media and documents
        ".mp3", ".mp4", ".wav", ".ogg", ".mov", ".avi", ".webm", ".pdf",
        # compiled
        ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".class", ".pyc", ".wasm", ".bin",
        # minified / generated
        ".map", ".lock",
    }
)  # fmt: skip
MINIFIED_SUFFIXES = (".min.js", ".min.css")

router = APIRouter(prefix="/api")


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: int = Field(gt=0)
    ref: str | None = Field(default=None, max_length=255)


class SessionOut(BaseModel):
    id: UUID
    repo_id: int
    repo_full_name: str
    ref: str
    commit_sha: str
    private: bool
    status: SessionStatus
    status_reason: str | None
    shared: bool
    created_at: datetime
    owner: str  # the owner's GitHub login


class StepOut(BaseModel):
    id: UUID
    status: StepStatus
    parent_node_id: UUID | None  # None for the Overview step


class SessionDetail(SessionOut):
    """GET /api/sessions/{id}: the session plus its whole graph."""

    nodes: list[GraphNode]  # ordered by created_at, id
    steps: list[StepOut]  # ordered by created_at, id


def session_out(session: Session, owner_login: str) -> SessionOut:
    return SessionOut(
        id=session.id,
        repo_id=session.repo_id,
        repo_full_name=session.repo_full_name,
        ref=session.ref,
        commit_sha=session.commit_sha,
        private=session.private,
        status=session.status,
        status_reason=session.status_reason,
        shared=session.shared,
        created_at=session.created_at,
        owner=owner_login,
    )


# --- tree size check (step 5) ---------------------------------------------------------


def is_source_file(path: str) -> bool:
    """SPEC's source-file rule: not under a vendored directory, not binary or minified."""
    parts = PurePosixPath(path).parts
    if any(part in VENDORED_DIRS for part in parts[:-1]):
        return False
    name = parts[-1].lower() if parts else ""
    if name.endswith(MINIFIED_SUFFIXES):
        return False
    return PurePosixPath(name).suffix not in BINARY_EXTENSIONS


def tree_refusal(tree: Tree) -> dict[str, Any] | None:
    """The 422 detail when the tree is over a limit, else None."""
    if tree.truncated:
        return {"reason": "tree_truncated"}
    blobs = [e for e in tree.entries if e.type == "blob"]
    files = sum(1 for e in blobs if is_source_file(e.path))
    if files > MAX_SOURCE_FILES:
        return {"reason": "too_many_files", "source_files": files, "limit": MAX_SOURCE_FILES}
    total = sum(e.size or 0 for e in blobs)
    if total > MAX_TREE_BYTES:
        return {"reason": "repo_too_large", "bytes": total, "limit": MAX_TREE_BYTES}
    return None


# --- background preparation (step 7) --------------------------------------------------


def safe_reason(exc: BaseException) -> str:
    """A fixed failure reason for the session row: never the exception's text."""
    if isinstance(exc, SnapshotError):
        return "snapshot_fetch_failed"
    if isinstance(exc, GitHubRateLimitedError):
        return "github_rate_limited"
    if isinstance(exc, GitHubError):
        return "github_unavailable"
    return "internal_error"


class Preparer:
    """Runs session preparation in the background and keeps strong refs to its tasks."""

    def __init__(self, auth: AuthState, github: GitHubApp, cache: RepoCache, depth: int) -> None:
        self.auth = auth
        self.github = github
        self.cache = cache
        self.depth = depth
        self.tasks: set[asyncio.Task[None]] = set()
        self.runner: StepRunner | None = None  # set by the app; starts the Overview

    def start(self, session: Session) -> asyncio.Task[None]:
        task = asyncio.create_task(self.prepare(session))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def snapshot_fetch(self, session: Session) -> Fetch:
        """The `fetch` for `RepoCache.lease` of this session's snapshot."""
        target = RepoTarget(id=session.repo_id, installation_id=session.installation_id)
        sha = session.commit_sha

        async def fetch(dest: Path) -> None:
            # Mints a fresh installation token on every call, so a re-fetch after eviction
            # works long after the session was created.
            await self.github.fetch_snapshot(target, sha, dest, self.depth)

        return fetch

    async def prepare(self, session: Session) -> None:
        """Lease the snapshot (fetching it if needed), check its size, set ready/failed,
        and on ready start the Overview step. The lease is held only while preparing;
        steps take their own."""
        sha = session.commit_sha
        fetch = self.snapshot_fetch(session)

        status: SessionStatus
        reason: str | None = None
        try:
            async with self.cache.lease(session.repo_id, sha, fetch):
                size = self.cache.size_of(session.repo_id, sha)
            if size is None or size > MAX_SNAPSHOT_BYTES:
                status, reason = "failed", "snapshot_too_large"
            else:
                status = "ready"
        except Exception as exc:
            status, reason = "failed", safe_reason(exc)
            log.warning("session %s preparation failed (%s)", session.id, type(exc).__name__)
        # Only the preparation that moved the session out of `preparing` starts the
        # Overview, so it starts at most once per session.
        if await self._set(session.id, status, reason) and status == "ready":
            await self.start_overview(session.model_copy(update={"status": "ready"}))

    async def _set(self, session_id: UUID, status: SessionStatus, reason: str | None) -> bool:
        maker = self.auth.sessionmaker
        if maker is None:
            log.error("session %s: no database to record %s", session_id, status)
            return False
        try:
            async with maker() as db:
                applied = await repo.finish_preparing(db, session_id, status, reason)
            if not applied:
                log.info("session %s no longer preparing; %s not recorded", session_id, status)
            return applied
        except Exception:
            # The session may have been purged meanwhile.
            log.exception("session %s: could not record status %s", session_id, status)
            return False

    async def start_overview(self, session: Session) -> None:
        """Admit the session's Overview step (no parent node: it hangs off the root)."""
        maker = self.auth.sessionmaker
        if maker is None or self.runner is None:
            return
        try:
            async with maker() as db:
                owner = await auth_store.get_user(db, session.owner_id)
                if owner is None:
                    return
                await self.runner.start_overview(db, session, owner)
        except Exception as exc:
            log.warning("session %s: Overview not started (%s)", session.id, type(exc).__name__)

    async def recover(self) -> int:
        """Startup: sessions left `preparing` by a previous process become failed."""
        maker = self.auth.sessionmaker
        if maker is None:
            return 0
        async with maker() as db:
            count = await repo.fail_preparing_sessions(db, INTERRUPTED)
        if count:
            log.info("marked %d interrupted session preparation(s) as failed", count)
        return count

    async def shutdown(self, timeout: float) -> None:
        """Cancel running preparations and wait for them, bounded by `timeout` seconds."""
        pending = set(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=timeout)


def preparer(request: Request) -> Preparer:
    prep: Preparer = request.app.state.preparer
    return prep


# --- routes ---------------------------------------------------------------------------


def _refuse(reason: str, **extra: Any) -> HTTPException:
    return HTTPException(422, {"reason": reason, **extra})


def _unavailable() -> HTTPException:
    return HTTPException(503, "GitHub is unavailable, try again")


def _not_found_status(exc: GitHubError) -> bool:
    return (
        isinstance(exc, GitHubHTTPError)
        and not isinstance(exc, GitHubRateLimitedError)
        and exc.status in (404, 422)
    )


async def _resolve(github: GitHubApp, target: RepoTarget, default_branch: str, ref: str) -> str:
    """Step 4: `ref` -> full SHA. Anything but a branch or tag name of the repo must be
    reachable from the default branch."""
    try:
        sha = await github.resolve_ref(target, ref)
        if ref == default_branch or await github.ref_exists(target, ref):
            return sha
    except GitHubError as exc:
        if _not_found_status(exc):
            raise _refuse("ref_not_found") from None
        raise _unavailable() from None
    try:
        status = await github.compare_status(target, default_branch, sha)
    except GitHubError as exc:
        if _not_found_status(exc):  # e.g. a commit GitHub cannot compare with the branch
            raise _refuse("ref_not_on_default_branch") from None
        raise _unavailable() from None
    if status not in ("identical", "behind"):
        raise _refuse("ref_not_on_default_branch")
    return sha


@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSession, request: Request, user: CurrentUser, auth: Auth, db: DB
) -> SessionOut:
    # 1. The repo must be in the user's listing (it gives the installation), then both
    #    parts of repo access.
    listed = await repo_list(request).repos(auth, db, user)
    found = next((r for r in listed if r.id == body.repo_id), None)
    if found is None or not await check_repo_access(
        auth, db, user, found.id, found.installation_id
    ):
        raise HTTPException(404, "repo_not_found")
    github: GitHubApp = request.app.state.github_app
    target = RepoTarget(id=found.id, installation_id=found.installation_id)
    try:
        info = await github.lookup(target)  # current visibility, name, default branch
    except GitHubError as exc:
        if _not_found_status(exc):
            raise HTTPException(404, "repo_not_found") from None
        raise _unavailable() from None
    # 2. Private repo: the notice must have been acknowledged. Nothing is created.
    if info.private and user.private_repo_notice_ack_at is None:
        raise HTTPException(409, "private_repo_notice_required")
    # 4. Ref -> SHA.
    ref = (body.ref or "").strip() or info.default_branch
    try:
        check_ref(ref)
    except GitHubError:
        raise _refuse("bad_ref") from None
    sha = await _resolve(github, target, info.default_branch, ref)
    # 5. Tree size check.
    try:
        tree = await github.tree(target, sha)
    except GitHubError:
        raise _unavailable() from None
    refusal = tree_refusal(tree)
    if refusal is not None:
        raise HTTPException(422, refusal)
    # 6. The row, `preparing`; 7. preparation in the background.
    session, _root = await repo.create_session(
        db,
        owner_id=user.id,
        installation_id=found.installation_id,
        repo_id=found.id,
        repo_full_name=info.full_name,
        ref=ref,
        commit_sha=sha,
        private=info.private,
        model=user.settings.model,
    )
    preparer(request).start(session)
    return session_out(session, user.github_login)


@router.get("/sessions")
async def list_sessions(user: CurrentUser, auth: Auth, db: DB) -> list[SessionOut]:
    """The user's own sessions that they can still access, newest first."""
    out: list[SessionOut] = []
    for session in await repo.list_owned_sessions(db, user.id):
        if await has_repo_access(auth, db, user, session):
            out.append(session_out(session, user.github_login))
    return out


async def _owner_login(db: DB, user: User, session: Session) -> str:
    if session.owner_id == user.id:
        return user.github_login
    owner = await auth_store.get_user(db, session.owner_id)
    return owner.github_login if owner is not None else ""


@router.get("/sessions/{id}")
async def get_session(id: UUID, request: Request, user: CurrentUser, db: DB) -> SessionDetail:
    """The session, all its nodes (oldest first) and its steps."""
    session = await load_session(request, db, user, id, Action.VIEW)
    out = session_out(session, await _owner_login(db, user, session))
    nodes = await repo.load_nodes(db, session.id)
    steps = [
        StepOut(id=s.id, status=s.status, parent_node_id=s.parent_node_id)
        for s in await repo.session_steps(db, session.id)
    ]
    return SessionDetail(**out.model_dump(), nodes=nodes, steps=steps)

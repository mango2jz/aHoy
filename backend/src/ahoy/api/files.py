"""`GET /api/sessions/{id}/files`: a line range of one file of the session's snapshot, for
the code viewer (SPEC -> API; T9).

Order: authz (`READ_FILES`), the session must be `ready`, the range is checked, then the
snapshot is leased (re-fetched transparently if it was evicted) only for the read. The path
goes through the same resolution as the tools (`sandbox.paths.resolve`): a path outside the
snapshot, under `.git`, or missing all answer the same 404, so probing reveals nothing;
denylisted names answer 403 with no content, in every command mode. The read itself is
`tools.fs.read_file` (64 KiB cap, long lines clipped, NUL means binary).
"""

import asyncio
import logging
import stat
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ahoy.api.sessions import preparer
from ahoy.auth.deps import CurrentUser
from ahoy.auth.sessions import DB
from ahoy.authz import Action, load_session
from ahoy.github.repo_cache import RepoCache
from ahoy.sandbox.paths import PathError, Snapshot, resolve
from ahoy.tools import ToolError
from ahoy.tools.fs import (
    MAX_LINE_CHARS,
    MAX_READ_LINES,
    BinaryFileError,
    RangeError,
    ReadFileResult,
    read_file,
)

log = logging.getLogger("ahoy.files")

router = APIRouter(prefix="/api")


class FileOut(BaseModel):
    path: str  # resolved, relative to the snapshot root
    start_line: int
    end_line: int  # last line returned (start_line - 1 if none)
    total_lines: int | None
    lines: list[str]  # without line endings
    clipped_lines: int  # returned lines cut to MAX_LINE_CHARS
    truncated: bool  # the byte cap cut the requested range
    github_url: str


def _fail(status: int, reason: str) -> HTTPException:
    return HTTPException(status, {"reason": reason})


def _not_found() -> HTTPException:
    return _fail(404, "file_not_found")


def _split(content: str) -> list[str]:
    """`read_file` content -> lines without endings (only `\\n` ends a line, as there)."""
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _read(root: Path, path: str, start: int) -> tuple[ReadFileResult, list[str]]:
    """Resolve and read `path` in the snapshot at `root` (blocking; run in a thread)."""
    snapshot = Snapshot(root=root)
    try:
        target = resolve(snapshot, path)
    except PathError as exc:
        if exc.reason == "denylisted":
            raise _fail(403, "denylisted") from None
        raise _not_found() from None  # outside, `.git`, invalid: same as missing
    try:
        st = target.lstat()
    except OSError:  # missing, or ENOTDIR for "file.txt/x"
        raise _not_found() from None
    if not stat.S_ISREG(st.st_mode):
        raise _fail(422, "not_a_file")
    try:
        result = read_file(snapshot, path, start)
    except PathError:  # changed under us: treat like the checks above
        raise _not_found() from None
    except BinaryFileError:
        raise _fail(422, "binary_file") from None
    except RangeError:
        raise _fail(422, "bad_range") from None
    except ToolError:
        raise _not_found() from None
    return result, _split(result.content)


async def _read_to_end(root: Path, path: str, start: int) -> tuple[ReadFileResult, list[str]]:
    """`_read` in a thread. A thread cannot be stopped, so if the request is cancelled
    this waits for the thread before re-raising: the caller's lease outlives the read."""
    task = asyncio.ensure_future(asyncio.to_thread(_read, root, path, start))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # retrieved: the request is gone, its answer does not matter
        raise


@router.get("/sessions/{id}/files")
async def get_file(
    id: UUID,
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DB,
    path: str,
    start: int = 1,
    end: int | None = None,
) -> FileOut:
    session = await load_session(request, db, user, id, Action.READ_FILES)
    if session.status != "ready":
        raise HTTPException(409, {"reason": "session_not_ready"})
    if end is None:
        end = start + MAX_READ_LINES - 1
    if not 1 <= start <= end or end - start >= MAX_READ_LINES:
        raise _fail(422, "bad_range")

    cache: RepoCache = request.app.state.repo_cache
    fetch = preparer(request).snapshot_fetch(session)
    async with AsyncExitStack() as stack:
        try:
            lease = cache.lease(session.repo_id, session.commit_sha, fetch)
            root = await stack.enter_async_context(lease)
        except Exception as exc:  # fetch or publish failed; never echo its text
            log.warning("session %s snapshot unavailable (%s)", session.id, type(exc).__name__)
            raise HTTPException(503, {"reason": "snapshot_unavailable"}) from None
        result, lines = await _read_to_end(root, path, start)

    lines = lines[: end - start + 1]
    end_line = start + len(lines) - 1
    response.headers["Cache-Control"] = "private, no-store"
    return FileOut(
        path=result.path,
        start_line=start,
        end_line=end_line,
        total_lines=result.total_lines,
        lines=lines,
        clipped_lines=sum(len(line) > MAX_LINE_CHARS for line in lines),
        truncated=end_line < min(end, result.total_lines),
        github_url=(
            f"https://github.com/{session.repo_full_name}/blob/{session.commit_sha}/"
            f"{quote(result.path)}#L{start}-L{max(end_line, start)}"
        ),
    )

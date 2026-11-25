"""`list_dir` and `read_file` against a read-only snapshot.

Both resolve their path argument with `sandbox.paths.resolve`, so they refuse the same
set of paths. The tree walker never follows symlinks, always hides `.git` and
denylisted names, and honors `.gitignore` files (root and nested) via `pathspec`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

import pathspec
from pydantic import BaseModel

from ahoy.sandbox.paths import (
    Snapshot,
    has_git_component,
    is_denylisted,
    is_valid_utf8,
    relative,
    resolve,
)
from ahoy.tools import ToolError

MAX_DEPTH = 3
MAX_ENTRIES = 500
MAX_READ_LINES = 500
MAX_READ_BYTES = 64 * 1024  # content per read_file call, cut at a line boundary
MAX_LINE_CHARS = 2000  # longer lines are clipped with LINE_CLIP_MARKER
LINE_CLIP_MARKER = " […line clipped]"
_RAW_LINE_KEEP = MAX_LINE_CHARS * 4 + 4  # enough bytes for MAX_LINE_CHARS UTF-8 chars
_DRAIN_CHUNK = 64 * 1024
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
LFS_POINTER_MAX_BYTES = 1024

NOTE_SUBMODULE = "submodule (not fetched)"
NOTE_EMPTY_DIR = "empty directory"
NOTE_LFS = "Git LFS pointer (content not fetched)"


def note_non_utf8(count: int) -> str | None:
    if not count:
        return None
    return f"{count} entries with non-UTF-8 names were skipped"


EntryKind = Literal["file", "dir", "symlink"]


class DirEntry(BaseModel):
    path: str  # relative to the snapshot root, POSIX separators
    kind: EntryKind
    size: int | None = None  # bytes, files only
    note: str | None = None  # submodule / empty dir / LFS pointer


class ListDirResult(BaseModel):
    path: str
    depth: int
    entries: list[DirEntry]
    truncated: bool  # True if more than MAX_ENTRIES entries were found
    note: str | None = None  # e.g. entries skipped for non-UTF-8 names


class ReadFileResult(BaseModel):
    path: str
    start_line: int
    end_line: int  # last line included (start_line - 1 if no lines returned)
    total_lines: int
    truncated: bool  # True if the file continues past end_line (line or byte cap)
    clipped_lines: int = 0  # returned lines cut to MAX_LINE_CHARS
    content: str
    note: str | None = None


# --- shared walker (also used by search's Python fallback) -------------------------


@dataclass(frozen=True)
class _Ignore:
    base: str  # directory of the .gitignore, relative to root ("" for the root)
    spec: pathspec.GitIgnoreSpec


@dataclass(frozen=True)
class WalkEntry:
    rel: str
    abs: Path
    kind: EntryKind
    depth: int  # 1 = direct child of the walk start


def _load_ignore(dir_abs: Path, base_rel: str) -> _Ignore | None:
    gi = dir_abs / ".gitignore"
    try:
        if gi.is_symlink() or not gi.is_file():
            return None
        lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return _Ignore(base_rel, pathspec.GitIgnoreSpec.from_lines(lines))


def _is_ignored(rel: str, is_dir: bool, ignores: tuple[_Ignore, ...]) -> bool:
    for ig in ignores:
        if ig.base:
            if not rel.startswith(ig.base + "/"):
                continue
            sub = rel[len(ig.base) + 1 :]
        else:
            sub = rel
        if ig.spec.match_file(sub + "/" if is_dir else sub):
            return True
    return False


def _ancestor_ignores(snapshot: Snapshot, start: Path) -> tuple[_Ignore, ...]:
    """`.gitignore` specs of the root and each directory down to `start` (inclusive)."""
    parts = start.relative_to(snapshot.root).parts
    ignores: list[_Ignore] = []
    for i in range(len(parts) + 1):
        rel = "/".join(parts[:i])
        ig = _load_ignore(snapshot.root.joinpath(*parts[:i]), rel)
        if ig is not None:
            ignores.append(ig)
    return tuple(ignores)


@dataclass
class WalkStats:
    non_utf8_skipped: int = 0


def _scan(dir_abs: Path) -> Iterator[os.DirEntry[str]] | None:
    try:
        with os.scandir(dir_abs) as it:
            return iter(sorted(it, key=lambda e: e.name))
    except OSError:
        return None


def walk(
    snapshot: Snapshot, start: Path, max_depth: int, stats: WalkStats | None = None
) -> Iterator[WalkEntry]:
    """Depth-first, sorted walk below `start` (an already resolved directory).

    Iterative (explicit stack). Never follows symlinks (they are yielded as kind
    "symlink" and not descended); skips `.git`, denylisted names, `.gitignore`d paths
    and names that are not valid UTF-8 (counted in `stats`).
    """
    stack: list[tuple[Iterator[os.DirEntry[str]], int, tuple[_Ignore, ...]]] = []
    first = _scan(start)
    if first is not None:
        stack.append((first, 1, _ancestor_ignores(snapshot, start)))
    while stack:
        entries, depth, ignores = stack[-1]
        e = next(entries, None)
        if e is None:
            stack.pop()
            continue
        if not is_valid_utf8(e.name):
            if stats is not None:
                stats.non_utf8_skipped += 1
            continue
        if e.name.lower() == ".git":
            continue
        abs_path = Path(e.path)
        rel = abs_path.relative_to(snapshot.root).as_posix()
        if is_denylisted(rel) or has_git_component(rel):
            continue
        try:
            if e.is_symlink():
                if not _is_ignored(rel, False, ignores):
                    yield WalkEntry(rel, abs_path, "symlink", depth)
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            is_file = not is_dir and e.is_file(follow_symlinks=False)
        except OSError:
            continue
        if _is_ignored(rel, is_dir, ignores):
            continue
        if is_dir:
            yield WalkEntry(rel, abs_path, "dir", depth)
            if depth < max_depth:
                children = _scan(abs_path)
                if children is not None:
                    child = _load_ignore(abs_path, rel)
                    sub = (*ignores, child) if child is not None else ignores
                    stack.append((children, depth + 1, sub))
        elif is_file:
            yield WalkEntry(rel, abs_path, "file", depth)


# --- repo map notes ----------------------------------------------------------------


def submodule_paths(snapshot: Snapshot) -> frozenset[str]:
    """Paths declared in the root `.gitmodules` (submodules are never fetched)."""
    gm = snapshot.root / ".gitmodules"
    try:
        if gm.is_symlink() or not gm.is_file():
            return frozenset()
        text = gm.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    paths: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "path":
            paths.add(value.strip().strip("/"))
    return frozenset(paths)


def is_lfs_pointer(path: Path) -> bool:
    """True if `path` is a small file that starts with the Git LFS pointer header."""
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > LFS_POINTER_MAX_BYTES:
            return False
        with path.open("rb") as f:
            return f.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def entry_note(entry: WalkEntry, submodules: frozenset[str]) -> str | None:
    if entry.kind == "dir":
        try:
            empty = not any(os.scandir(entry.abs))
        except OSError:
            return None
        if empty:
            return NOTE_SUBMODULE if entry.rel in submodules else NOTE_EMPTY_DIR
        return None
    if entry.kind == "file" and is_lfs_pointer(entry.abs):
        return NOTE_LFS
    return None


# --- tools -------------------------------------------------------------------------


def list_dir(snapshot: Snapshot, path: str = ".", depth: int = 1) -> ListDirResult:
    """List `path` up to `depth` levels (clamped to 1..3), honoring `.gitignore`."""
    depth = max(1, min(depth, MAX_DEPTH))
    target = resolve(snapshot, path)
    if not target.is_dir():
        raise ToolError(f"not a directory: {path!r}")
    submodules = submodule_paths(snapshot)
    entries: list[DirEntry] = []
    truncated = False
    stats = WalkStats()
    for w in walk(snapshot, target, depth, stats):
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        size: int | None = None
        if w.kind == "file":
            try:
                size = w.abs.lstat().st_size
            except OSError:
                size = None
        entries.append(DirEntry(path=w.rel, kind=w.kind, size=size, note=entry_note(w, submodules)))
    return ListDirResult(
        path=relative(snapshot, target),
        depth=depth,
        entries=entries,
        truncated=truncated,
        note=note_non_utf8(stats.non_utf8_skipped),
    )


def read_file(snapshot: Snapshot, path: str, start_line: int = 1) -> ReadFileResult:
    """Read a text file from `start_line` (1-based): at most 500 lines and 64 KiB.

    Lines longer than 2000 characters are clipped with a marker. `truncated` is True
    when the file continues past `end_line`, whichever cap stopped the read.
    """
    if start_line < 1:
        raise ToolError("start_line must be >= 1")
    target = resolve(snapshot, path)
    try:
        st = target.lstat()
    except FileNotFoundError as exc:
        raise ToolError(f"no such file: {path!r}") from exc
    except OSError as exc:  # e.g. ENOTDIR for "file.txt/x"
        raise ToolError(f"cannot open {path!r}: {exc.strerror}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ToolError(f"not a regular file: {path!r}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise ToolError(f"cannot open {path!r}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as f:
        lines: list[str] = []
        used = 0
        clipped = 0
        byte_capped = False
        total = 0
        last = start_line + MAX_READ_LINES - 1
        for raw, cut, has_nul in _raw_lines(f):
            if has_nul:  # any NUL anywhere in the file means binary
                raise ToolError(f"binary file: {path!r}")
            total += 1
            if byte_capped or not start_line <= total <= last:
                continue
            line, was_clipped = _clip_line(raw, cut)
            size = len(line.encode("utf-8"))
            if used + size > MAX_READ_BYTES:
                byte_capped = True
                continue
            lines.append(line)
            used += size
            clipped += was_clipped

    if total and start_line > total:
        raise ToolError(f"start_line {start_line} is past the end ({total} lines)")
    end_line = start_line + len(lines) - 1
    return ReadFileResult(
        path=relative(snapshot, target),
        start_line=start_line,
        end_line=end_line,
        total_lines=total,
        truncated=end_line < total,
        clipped_lines=clipped,
        content="".join(lines),
        note=NOTE_LFS if is_lfs_pointer(target) else None,
    )


def _raw_lines(f: BinaryIO) -> Iterator[tuple[bytes, bool, bool]]:
    """Yield (head, cut, has_nul) per line, keeping at most _RAW_LINE_KEEP bytes.

    A longer line's remainder is read and discarded (but still checked for NUL), so
    memory stays bounded even for a multi-megabyte single-line file; `cut` is True
    for such lines. The line ending (`\\n` or `\\r\\n`) is kept on cut lines.
    """
    while True:
        head = f.readline(_RAW_LINE_KEEP)
        if not head:
            return
        if head.endswith(b"\n") or len(head) < _RAW_LINE_KEEP:
            yield head, False, b"\x00" in head
            continue
        has_nul = b"\x00" in head
        tail = b""
        while True:
            rest = f.readline(_DRAIN_CHUNK)
            if not rest:
                break
            has_nul = has_nul or b"\x00" in rest
            tail = (tail + rest)[-2:]
            if rest.endswith(b"\n"):
                break
        ending = b"\r\n" if tail.endswith(b"\r\n") else b"\n" if tail.endswith(b"\n") else b""
        yield head.rstrip(b"\r") + ending, True, has_nul


def _clip_line(raw: bytes, cut: bool) -> tuple[str, bool]:
    text = raw.decode("utf-8", errors="replace")
    ending = "\r\n" if text.endswith("\r\n") else "\n" if text.endswith("\n") else ""
    body = text[: len(text) - len(ending)]
    if cut or len(body) > MAX_LINE_CHARS:
        return body[:MAX_LINE_CHARS] + LINE_CLIP_MARKER + ending, True
    return text, False

"""`search`: literal text search over the snapshot, at most 50 hits.

Uses ripgrep when it is on PATH, otherwise a pure-Python walk. Both engines search
hidden files, honor `.gitignore`, skip `.git`, binary files, files over 5 MB and
denylisted names, and never follow symlinks. Every hit is re-checked against the
denylist and `resolve()` before it is returned.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Literal

import pathspec
from pydantic import BaseModel

from ahoy.sandbox.paths import DENYLIST_PATTERNS, PathError, Snapshot, is_denylisted, resolve
from ahoy.tools import ToolError
from ahoy.tools.fs import WalkStats, note_non_utf8, walk

MAX_HITS = 50
MAX_LINE_CHARS = 300
MAX_FILE_BYTES = 5 * 1024 * 1024
RG_TIMEOUT_S = 15.0
_UNLIMITED_DEPTH = 10_000


class SearchHit(BaseModel):
    path: str
    line: int
    text: str


class SearchResult(BaseModel):
    query: str
    glob: str | None
    hits: list[SearchHit]
    truncated: bool  # True if there were more than MAX_HITS hits
    engine: Literal["rg", "python"]
    note: str | None = None  # e.g. files skipped for non-UTF-8 names


def find_rg() -> str | None:
    """Path of the `rg` executable, or None. Tests monkeypatch this to force the fallback."""
    return shutil.which("rg")


def search(snapshot: Snapshot, query: str, glob: str | None = None) -> SearchResult:
    """Find lines containing `query` (literal, case-sensitive), optionally only in `glob`."""
    if not query:
        raise ToolError("query must not be empty")
    if "\x00" in query or "\n" in query:
        raise ToolError("query must be a single line without NUL bytes")
    if glob is not None and ("\x00" in glob or not glob.strip()):
        raise ToolError("invalid glob")

    rg = find_rg()
    stats = WalkStats()
    if rg is not None:
        hits, more = _search_rg(rg, snapshot, query, glob, stats)
        engine: Literal["rg", "python"] = "rg"
    else:
        hits, more = _search_python(snapshot, query, glob, stats)
        engine = "python"
    return SearchResult(
        query=query,
        glob=glob,
        hits=hits,
        truncated=more,
        engine=engine,
        note=note_non_utf8(stats.non_utf8_skipped),
    )


def _accept(snapshot: Snapshot, rel: str) -> bool:
    """A hit path is returned only if it is clean, inside the root and a regular file."""
    if is_denylisted(rel):
        return False
    try:
        st = (snapshot.root / rel).lstat()  # lstat: a symlink is never a regular file
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        resolve(snapshot, rel)
    except PathError:
        return False
    return True


def _clip(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " […]"


# --- ripgrep -----------------------------------------------------------------------


def _rg_argv(rg: str, query: str, glob: str | None) -> list[str]:
    argv = [
        rg,
        "--no-config",
        "--no-messages",
        "--color=never",
        "--json",
        "--fixed-strings",
        "--case-sensitive",
        "--hidden",
        "--no-require-git",
        "--sort=path",
        f"--max-count={MAX_HITS + 1}",
        "--max-filesize=5M",
        f"--max-columns={MAX_LINE_CHARS}",
        "--max-columns-preview",
    ]
    if glob is not None:
        argv += ["--glob", glob]
    # Later globs take precedence in rg, so the excludes come after the user's glob.
    argv += ["--iglob", "!.git"]
    for pat in DENYLIST_PATTERNS:
        argv += ["--iglob", f"!{pat}"]
    argv += ["-e", query, "--", "."]
    return argv


def _rg_match(raw: bytes, stats: WalkStats) -> tuple[str, int, str] | None:
    """(path, line, text) from one `rg --json` line if it is a `match` event, else None.

    Only `type == "match"` events are read; rg's other events and any non-JSON
    output (such as binary-file warnings) are ignored. A path reported as
    `path.bytes` is not valid UTF-8 and is counted as skipped; a line reported as
    `lines.bytes` is dropped.
    """
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") != "match":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path, lines, lineno = data.get("path"), data.get("lines"), data.get("line_number")
    if not isinstance(path, dict) or not isinstance(lineno, int):
        return None
    name = path.get("text")
    if not isinstance(name, str):
        if "bytes" in path:
            stats.non_utf8_skipped += 1
        return None
    text = lines.get("text") if isinstance(lines, dict) else None
    if not isinstance(text, str):  # `lines.bytes`: the line is not valid UTF-8; drop it
        return None
    return name.removeprefix("./"), lineno, text


def _search_rg(
    rg: str, snapshot: Snapshot, query: str, glob: str | None, stats: WalkStats
) -> tuple[list[SearchHit], bool]:
    proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, fixed flags
        _rg_argv(rg, query, glob),
        cwd=snapshot.root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    stdout = proc.stdout
    if stdout is None:  # pragma: no cover - stdout=PIPE always sets it
        proc.kill()
        raise ToolError("search failed")
    timer = threading.Timer(RG_TIMEOUT_S, proc.kill)
    timer.start()
    hits: list[SearchHit] = []
    text_ok: dict[str, bool] = {}
    more = False
    stopped = False
    try:
        for raw in stdout:
            match = _rg_match(raw, stats)
            if match is None:
                continue
            rel, lineno, text = match
            if rel not in text_ok:
                # rg reports matches that precede a file's first NUL; like the fallback,
                # treat a NUL anywhere as binary and drop the file.
                ok = _accept(snapshot, rel)
                text_ok[rel] = ok and _text_bytes(snapshot.root / rel) is not None
            if not text_ok[rel]:
                continue
            if len(hits) >= MAX_HITS:
                more = True
                stopped = True
                proc.kill()
                break
            hits.append(
                SearchHit(
                    path=rel,
                    line=lineno,
                    text=_clip(text),
                )
            )
    finally:
        timer.cancel()
        stdout.close()
        rc = proc.wait()
    if not stopped and rc not in (0, 1) and not hits:
        raise ToolError("search failed")
    return hits, more


# --- pure-Python fallback ----------------------------------------------------------


def _search_python(
    snapshot: Snapshot, query: str, glob: str | None, stats: WalkStats
) -> tuple[list[SearchHit], bool]:
    spec = pathspec.GitIgnoreSpec.from_lines([glob]) if glob is not None else None
    hits: list[SearchHit] = []
    for entry in walk(snapshot, snapshot.root, _UNLIMITED_DEPTH, stats):
        if entry.kind != "file":
            continue
        if spec is not None and not spec.match_file(entry.rel):
            continue
        if not _accept(snapshot, entry.rel):
            continue
        for lineno, text in _matching_lines(entry.abs, query):
            if len(hits) >= MAX_HITS:
                return hits, True
            hits.append(SearchHit(path=entry.rel, line=lineno, text=_clip(text)))
    return hits, False


def _text_bytes(path: Path) -> bytes | None:
    """The file's bytes, or None if it is over MAX_FILE_BYTES or binary (any NUL)."""
    try:
        if path.lstat().st_size > MAX_FILE_BYTES:
            return None
        with path.open("rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES or b"\x00" in data:
        return None
    return data


def _matching_lines(path: Path, query: str) -> list[tuple[int, str]]:
    data = _text_bytes(path)
    if data is None:
        return []
    needle = query.encode("utf-8")
    hits: list[tuple[int, str]] = []
    for n, raw in enumerate(data.split(b"\n"), start=1):
        if needle in raw:
            hits.append((n, raw.decode("utf-8", errors="replace")))
            if len(hits) > MAX_HITS:
                break
    return hits

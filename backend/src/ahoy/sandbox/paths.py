"""Path resolution and the denylist for the read-only repo snapshot.

Every tool that touches the snapshot resolves user (model) supplied paths through
`resolve()`. It walks the path one component at a time, resolving each symlink it
meets, so no intermediate or final symlink can lead outside the snapshot root.
`is_denylisted()` is the single denylist matcher shared by the tools and the gate.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

# SPEC "Command modes": `.env*`, `*.pem`, `*.key`, `id_*`. Matched against every path
# component, case-insensitively (snapshots may live on case-insensitive filesystems,
# and `x.PEM` is as secret as `x.pem`).
DENYLIST_PATTERNS: tuple[str, ...] = (".env*", "*.pem", "*.key", "id_*")

PathErrorReason = Literal[
    "nul", "absolute", "escape", "symlink_escape", "denylisted", "git_dir", "invalid"
]


class PathError(ValueError):
    """A path was refused. `reason` is a stable code; the message is safe to show the model."""

    def __init__(self, reason: PathErrorReason, path: str, detail: str = "") -> None:
        self.reason: PathErrorReason = reason
        self.path = path
        msg = f"path refused ({reason}): {path!r}"
        super().__init__(f"{msg}: {detail}" if detail else msg)


@dataclass(frozen=True)
class Snapshot:
    """A read-only repo snapshot. `root` is normalized to an absolute, resolved directory."""

    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"snapshot root is not a directory: {root}")
        object.__setattr__(self, "root", root)


def is_denylisted(rel_path: str | PurePosixPath) -> bool:
    """True if any component of `rel_path` matches the denylist (case-insensitive)."""
    for part in PurePosixPath(rel_path).parts:
        name = part.lower()
        if any(fnmatch.fnmatchcase(name, pat) for pat in DENYLIST_PATTERNS):
            return True
    return False


def has_git_component(rel_path: str | PurePosixPath) -> bool:
    """True if any component of `rel_path` is `.git` (case-insensitive)."""
    return any(part.lower() == ".git" for part in PurePosixPath(rel_path).parts)


def is_valid_utf8(name: str) -> bool:
    """False for names holding lone surrogates (undecodable bytes via surrogateescape)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def resolve(snapshot: Snapshot, path: str) -> Path:
    """Resolve `path` (relative to the snapshot root) to an absolute path inside it.

    Raises PathError for NUL bytes, absolute paths, `..` escapes, symlinks (at any
    component) whose target leaves the root, symlink loops, non-UTF-8 text, `.git`
    components and denylisted names, both as written and after symlink resolution.
    The returned path contains no symlinks; it may not exist.
    """
    if "\x00" in path:
        raise PathError("nul", path.replace("\x00", "\\0"))
    if path.startswith(("/", "\\")) or PurePosixPath(path).is_absolute():
        raise PathError("absolute", path)
    if not is_valid_utf8(path):
        raise PathError("invalid", path.encode("utf-8", "backslashreplace").decode(), "not UTF-8")
    if is_denylisted(path):
        raise PathError("denylisted", path)
    if has_git_component(path):
        raise PathError("git_dir", path)

    root = snapshot.root
    cur = root
    for part in PurePosixPath(path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if cur == root:
                raise PathError("escape", path)
            cur = cur.parent
            continue
        nxt = cur / part
        try:
            is_link = nxt.is_symlink()
        except OSError as exc:  # ENAMETOOLONG and friends
            raise PathError("invalid", path, exc.strerror or "") from exc
        if is_link:
            try:
                target = nxt.resolve(strict=False)
            except (OSError, RuntimeError) as exc:  # ELOOP (3.13+) / RuntimeError (3.12)
                raise PathError("invalid", path, "symlink loop") from exc
            if target.is_symlink():  # non-strict resolve() stops at a loop on 3.13+
                raise PathError("invalid", path, "symlink loop")
            if not _inside(target, root):
                raise PathError("symlink_escape", path)
            cur = target
        else:
            cur = nxt

    # Belt and braces: the fully resolved result must be inside the root and clean.
    try:
        final = cur.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathError("invalid", path, "symlink loop") from exc
    if not _inside(final, root):
        raise PathError("symlink_escape", path)
    final_rel = final.relative_to(root).as_posix()
    if is_denylisted(final_rel):
        raise PathError("denylisted", path)
    if has_git_component(final_rel):
        raise PathError("git_dir", path)
    return final


def relative(snapshot: Snapshot, abs_path: Path) -> str:
    """POSIX path of `abs_path` relative to the snapshot root ("." for the root)."""
    rel = abs_path.relative_to(snapshot.root).as_posix()
    return rel or "."

"""The command gate: decides whether a model-proposed shell command may run.

Read-only blocks every command. Safe runs only single, simple, allowlisted commands
(as an argv list, no shell) and enforces the Safe-mode flag denylist. Auto runs every
command through `/bin/sh -c` in the sandbox. The file denylist applies in every mode.
See SPEC.md -> "Command modes" and "Tools".
"""

from __future__ import annotations

import contextlib
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Literal

from ahoy.harness.schemas import DEFAULT_SAFE_ALLOWLIST, CommandMode
from ahoy.sandbox.paths import DENYLIST_PATTERNS, is_denylisted

Mode = CommandMode

# Anything a shell or git could treat as a separator around a path name:
# path separators, `--opt=value`, `<rev>:<path>`, pathspec magic `:(top)` / `:!`,
# quotes, whitespace and shell punctuation.
_NAME_SPLIT = re.compile(r"[/\\=:,!()^@{}'\"\s;|&<>$`]+")

# Blocked anywhere in a Safe-mode command, before parsing.
_SAFE_FORBIDDEN: tuple[str, ...] = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "\x00")

# For each pattern: the file itself, and everything under a directory of that name.
GIT_EXCLUDE_PATHSPECS: tuple[str, ...] = tuple(
    spec
    for pattern in DENYLIST_PATTERNS
    for spec in (f":(exclude,icase,glob)**/{pattern}", f":(exclude,icase,glob)**/{pattern}/**")
)
# Subcommands that get the excludes appended (`status -vv` prints index diffs).
_GIT_PATHSPEC_SUBCOMMANDS = frozenset({"status", "log", "diff", "show"})
# The only git subcommands Safe mode runs, whatever the allowlist says. Others
# (grep --cached, archive, format-patch, stash, cat-file, ...) read the index or
# objects without honouring the excludes.
_GIT_SAFE_SUBCOMMANDS = _GIT_PATHSPEC_SUBCOMMANDS | {"blame"}


@dataclass(frozen=True)
class FlagRule:
    """Flags a program may not receive in Safe mode.

    `exact`: whole-token matches (find predicates).
    `long`: `--name`, `--name=value` and any abbreviation (`--na`) of `--name`.
    `short`: single letters, also inside clusters like `-iz`.
    """

    exact: frozenset[str] = frozenset()
    long: tuple[str, ...] = ()
    short: str = ""


# SPEC "Safe-mode flag denylist", as data. Keyed by the program's basename.
# The `git` global-option row and the `git show <rev>:<path>` row are code below.
FLAG_DENYLIST: dict[str, FlagRule] = {
    "find": FlagRule(
        exact=frozenset(
            {
                "-exec",
                "-execdir",
                "-ok",
                "-okdir",
                "-delete",
                "-fprint",
                "-fprint0",
                "-fprintf",
                "-fls",
            }
        )
    ),
    # `--hostname-bin` is beyond SPEC: it makes rg run a program.
    "rg": FlagRule(long=("--pre", "--pre-glob", "--search-zip", "--hostname-bin"), short="z"),
    # `-R` is beyond SPEC: with `-H` it writes 00Tree.html into every directory.
    "tree": FlagRule(short="oR"),
    "git": FlagRule(
        long=(
            "--ext-diff",
            "--textconv",
            "--output",
            "--upload-pack",
            "--open-files-in-pager",
            # Beyond SPEC: shows every file of a matched commit, ignoring the excludes.
            "--full-diff",
        )
    ),
}


@dataclass(frozen=True)
class Policy:
    """The owner's command policy. `allowlist` entries are command prefixes (`git log`)."""

    allowlist: tuple[str, ...] = DEFAULT_SAFE_ALLOWLIST


@dataclass(frozen=True)
class Decision:
    kind: Literal["run", "block"]
    reason: str
    argv: list[str] | None = None
    shell_cmd: str | None = None

    @property
    def blocked(self) -> bool:
        return self.kind == "block"


@dataclass(frozen=True)
class _Verdict:
    """Internal Safe-mode result: either an argv or a block reason."""

    argv: list[str] | None = None
    reason: str = ""


def _block(reason: str) -> Decision:
    return Decision(kind="block", reason=reason)


def _token_names_denylisted(token: str) -> bool:
    """True if any name component of `token` matches the denylist (case-insensitive).

    For option-like components (`-f.env`) every suffix is tried too, so a value
    glued onto a short flag is still caught.
    """
    for part in _NAME_SPLIT.split(token):
        if is_denylisted(part):
            return True
        if part.startswith("-") and any(is_denylisted(part[i:]) for i in range(1, len(part))):
            return True
    return False


def names_denylisted_path(cmd: str) -> bool:
    """True if `cmd` names a denylisted path, judged on the raw string and its shlex tokens."""
    tokens = [cmd]
    with contextlib.suppress(ValueError):  # unbalanced quotes: the raw string is still scanned
        tokens.extend(shlex.split(cmd))
    return any(_token_names_denylisted(t) for t in tokens)


def _blocked_flag(argv: list[str], rule: FlagRule) -> str | None:
    for token in argv[1:]:
        if token in rule.exact:
            return token
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if len(name) > 2 and any(opt.startswith(name) for opt in rule.long):
                return token
        elif token.startswith("-") and any(c in rule.short for c in token[1:]):
            return token
    return None


def _allowlisted(argv: list[str], allowlist: tuple[str, ...]) -> bool:
    for entry in allowlist:
        words = entry.split()
        if words and argv[: len(words)] == words:
            return True
    return False


def _check_safe(cmd: str, policy: Policy) -> _Verdict:
    for bad in _SAFE_FORBIDDEN:
        if bad in cmd:
            return _Verdict(reason=f"Safe mode: {bad!r} (chaining, redirection or substitution)")
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return _Verdict(reason="Safe mode: the command cannot be parsed (unbalanced quotes)")
    if not argv:
        return _Verdict(reason="Safe mode: empty command")

    program = posixpath.basename(argv[0])
    if program == "git" and len(argv) > 1 and argv[1].startswith("-"):
        return _Verdict(reason=f"Safe mode: git global option {argv[1]!r} before the subcommand")
    rule = FLAG_DENYLIST.get(program)
    if rule is not None:
        flag = _blocked_flag(argv, rule)
        if flag is not None:
            return _Verdict(reason=f"Safe mode: {program} flag {flag!r} is not allowed")

    if not _allowlisted(argv, policy.allowlist):
        return _Verdict(reason=f"Safe mode: {argv[0]!r} is not on the allowlist")
    if program == "git":
        if len(argv) < 2:
            return _Verdict(reason="Safe mode: bare git without a subcommand")
        if argv[1] not in _GIT_SAFE_SUBCOMMANDS:
            return _Verdict(reason=f"Safe mode: git {argv[1]!r} is not a Safe git subcommand")
    if names_denylisted_path(cmd):
        return _Verdict(reason="the command names a denylisted path")

    if program == "git" and argv[1] in _GIT_PATHSPEC_SUBCOMMANDS:
        # Excludes are appended even after a caller's own pathspec: a glob pathspec
        # such as '.e*' would otherwise reach a denylisted file's history.
        tail = list(GIT_EXCLUDE_PATHSPECS) if "--" in argv else ["--", *GIT_EXCLUDE_PATHSPECS]
        argv = [*argv, *tail]
    return _Verdict(argv=argv)


def check(cmd: str, mode: Mode, policy: Policy, pre_authorized: frozenset[str]) -> Decision:
    """Decide whether `cmd` may run under `mode`.

    `pre_authorized` holds command strings the owner approved with "Allow blocked…";
    only an exact match lets a Safe-blocked command run (via the shell). The file
    denylist is never bypassed.
    """
    if mode == "read_only":
        return _block("Read-only mode: no shell commands")
    if mode == "auto":
        if names_denylisted_path(cmd):
            return _block("the command names a denylisted path")
        return Decision(kind="run", reason="Auto mode", shell_cmd=cmd)
    if mode != "safe":
        return _block(f"unknown mode {mode!r}")

    verdict = _check_safe(cmd, policy)
    if verdict.argv is not None:
        return Decision(kind="run", reason="Safe mode: allowlisted", argv=verdict.argv)
    if cmd in pre_authorized:
        if names_denylisted_path(cmd):
            return _block(
                "the command names a denylisted path (pre-authorization does not lift it)"
            )
        return Decision(kind="run", reason="pre-authorized by the owner", shell_cmd=cmd)
    return _block(verdict.reason)

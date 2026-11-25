"""Safe-mode git vectors against real git on a repo built like a snapshot:
history with denylisted files (also under denylisted directories and in upper case,
plus a rename out of a denylisted name), then the denylisted files deleted from the
working tree. Each vector must be blocked, or run without printing any secret."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ahoy.harness.gate import Policy, check

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

# Markers that only exist inside denylisted files. S7_REN4 is the line dropped when
# `.env.old` was renamed to `notes.txt` (S7_REN1..3 legitimately live in notes.txt).
SECRET = re.compile(r"S[1-6]_[A-Z]+|S7_REN4")
DEFAULT = Policy()
ALL_GIT = Policy(allowlist=("git",))
DENYLISTED = {
    ".env": "S1_ENV\n",
    ".envs/.production/x": "S2_DIRENV\n",
    "certs.pem/a": "S3_DIRPEM\n",
    "X.PEM": "S4_UPPEM\n",
    "sub/.ENV": "S5_UPENV\n",
    "keys/id_rsa": "S6_KEY\n",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent / "home"),
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("snap") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "README", "pub\n")
    for rel, text in DENYLISTED.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _write(repo, ".env.old", "S7_REN1\nS7_REN2\nS7_REN3\nS7_REN4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    _git(repo, "mv", ".env.old", "notes.txt")
    _write(repo, "notes.txt", "S7_REN1\nS7_REN2\nS7_REN3\nchanged\n")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-qm", "three").returncode == 0
    # Strip the snapshot, as fetch_snapshot does (index and history keep them).
    for rel in DENYLISTED:
        (repo / rel).unlink()
    return repo


def _run_safe(repo: Path, cmd: str, policy: Policy = DEFAULT) -> str | None:
    """Gate `cmd`; if it runs, execute its argv and return the output (None if blocked)."""
    d = check(cmd, "safe", policy, frozenset())
    if d.blocked:
        return None
    assert d.argv is not None and d.argv[0] == "git"
    out = _git(repo, *d.argv[1:])
    return out.stdout + out.stderr


@pytest.mark.parametrize(
    "cmd",
    [
        "git log --all -p",
        "git show HEAD~2",
        "git status -vv",
        "git diff",
        "git log -p --all",
    ],
)
def test_fixture_really_leaks_without_the_gate(snapshot: Path, cmd: str) -> None:
    out = _git(snapshot, *cmd.split()[1:])
    assert SECRET.search(out.stdout), cmd


# Vectors that run, with the excludes appended, and must print no secret.
RUN_CLEAN = [
    "git status",
    "git status -vv",  # index diff of deleted files
    "git status -vv --untracked-files=all",
    "git status --porcelain",
    "git diff",  # working tree vs index: deleted files
    "git diff --cached",
    "git diff --staged",
    "git diff HEAD",
    "git diff HEAD~2 HEAD",  # files under denylisted directories
    "git diff HEAD~2 HEAD -M -C -C",
    "git diff HEAD~2 HEAD -B -M",
    "git diff -R",
    "git diff --relative",
    "git diff --irreversible-delete",
    "git log -p",
    "git log --all -p",
    "git log -p --all --source --full-history",
    "git log -p -M",  # rename out of a denylisted name
    "git log -p --no-renames",
    "git log -p -C -C --find-copies-harder",
    "git log -p -B -M",
    "git log -p --reverse",
    "git log -p --walk-reflogs",
    "git log -p --diff-merges=on",
    "git log -p --pretty=raw",
    "git log --stat --all",
    "git log -S S1_ENV -p",  # pickaxe
    "git log -S S1_ENV -p --pickaxe-all",
    "git log -G S2 -p",
    "git log -p -- '*'",
    "git log -p -- '.e*'",  # caller glob pathspec
    "git log -p -- ':(icase)*.P?m'",
    "git show",
    "git show HEAD~2",
    "git show HEAD~1 --stat",
    "git show --cc HEAD",
    "git show :README",  # index stage of a non-denylisted path
    "git show HEAD:README",
    "git show HEAD:keys",  # tree listing: names only
    "git blame notes.txt",
    "git blame -C -C -C notes.txt",
]


@pytest.mark.parametrize("cmd", RUN_CLEAN)
def test_git_vector_runs_without_secrets(snapshot: Path, cmd: str) -> None:
    d = check(cmd, "safe", DEFAULT, frozenset())
    assert not d.blocked and d.argv is not None, f"expected {cmd!r} to run"
    proc = _git(snapshot, *d.argv[1:])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{cmd!r} exited {proc.returncode}: {proc.stderr}"
    assert not SECRET.search(out), f"{cmd!r} leaked: {SECRET.findall(out)}"


# Vectors that must be blocked (even when the allowlist contains plain `git`).
BLOCKED = [
    "git",  # bare git with allowlist ("git",)
    "git show :.env",  # index-stage path
    "git show :0:.env",
    "git show :0:sub/.ENV",
    "git show HEAD~2:.envs/.production/x",  # file under a denylisted directory
    "git show HEAD~2:certs.pem/a",
    "git show HEAD~2:X.PEM",
    "git show HEAD~2:keys/id_rsa",
    "git show HEAD~1:.env.old",
    "git diff HEAD~2:.env HEAD:README",
    "git log -L1,1:.env.old",  # line-log
    "git log -p --full-diff -- README",
    "git log -p -- ':(icase)*.Pem'",  # pathspec naming a denylisted pattern
    "git blame HEAD~1 -- .env.old",
    "git blame --contents .env README",
    "git grep S1 --cached",
    "git grep --no-index S1",
    "git grep S1 HEAD~2",
    "git ls-files",
    "git cat-file -p HEAD~2:.env",
    "git cat-file --batch-all-objects --batch",
    "git stash show -p",
    "git archive HEAD~2",
    "git format-patch --stdout --root",
    "git rev-list --objects --all",
    "git ls-tree -r HEAD~2",
    "git -c core.pager=cat show HEAD~2",
    "git --literal-pathspecs log -p",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_git_vector_blocked(snapshot: Path, cmd: str) -> None:
    assert _run_safe(snapshot, cmd, ALL_GIT) is None


def test_line_log_on_a_clean_file_fails_closed(snapshot: Path) -> None:
    # -L cannot be combined with pathspecs, so the appended excludes make git refuse it.
    out = _run_safe(snapshot, "git log -L1,1:README")
    assert out is not None and "cannot be used with pathspec" in out

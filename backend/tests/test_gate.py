"""Gate tests: every mode, chaining, the Safe-mode flag denylist, the file denylist,
git exclude pathspecs and pre-authorization (SPEC success criterion 4)."""

from typing import Any

import pytest

from ahoy.harness import gate
from ahoy.harness.gate import GIT_EXCLUDE_PATHSPECS, Decision, Policy, check

POLICY = Policy()
NONE: frozenset[str] = frozenset()
EXCLUDES = list(GIT_EXCLUDE_PATHSPECS)


def safe(cmd: str, pre: frozenset[str] = NONE, policy: Policy = POLICY) -> Decision:
    return check(cmd, "safe", policy, pre)


# --- Success criterion 4 ---------------------------------------------------------

CRITERION_4_SAFE = [
    "ls; rm -rf x",
    "cat a | sh",
    "echo $(whoami)",
    "cat .env",
    r"find . -exec sh {} \;",
    "rg --pre sh x",
    "git show HEAD:.env",
]


@pytest.mark.parametrize("cmd", CRITERION_4_SAFE)
def test_criterion_4_blocked_in_safe(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked
    assert d.argv is None and d.shell_cmd is None


@pytest.mark.parametrize("cmd", [*CRITERION_4_SAFE, "ls", "git status", ""])
def test_read_only_blocks_everything(cmd: str) -> None:
    d = check(cmd, "read_only", POLICY, frozenset({cmd}))
    assert d.kind == "block"
    assert d.argv is None and d.shell_cmd is None


def test_auto_blocks_cat_env_by_name() -> None:
    d = check("cat .env", "auto", POLICY, NONE)
    assert d.blocked and "denylisted" in d.reason


# --- Safe: chaining, redirection, substitution, parsing ---------------------------

SAFE_BLOCKED_SYNTAX = [
    "ls && rm x",
    "ls || rm x",
    "ls & rm x",
    "ls &",
    "ls > out",
    "ls >> out",
    "cat < a",
    "echo `id`",
    "ls\nrm x",
    "ls\rrm x",
    "cat a\x00b",
    "cat 'unbalanced",
    'cat "unbalanced',
    "",
    "   ",
]


@pytest.mark.parametrize("cmd", SAFE_BLOCKED_SYNTAX)
def test_safe_blocks_syntax(cmd: str) -> None:
    assert safe(cmd).blocked


# --- Safe: allowlist -----------------------------------------------------------------

SAFE_NOT_ALLOWLISTED = [
    "rm -rf x",
    "sh -c ls",
    "python3 x.py",
    "FOO=1 ls",
    "env ls",
    "/usr/bin/find .",
    "/bin/ls",
    "./ls",
    "git",
    "git push",
    "git grep foo",
    "git fetch",
    "LS",
]


@pytest.mark.parametrize("cmd", SAFE_NOT_ALLOWLISTED)
def test_safe_blocks_not_allowlisted(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "allowlist" in d.reason


SAFE_ALLOWED: list[tuple[str, list[str]]] = [
    ("ls -la src", ["ls", "-la", "src"]),
    ("cat README.md", ["cat", "README.md"]),
    ("head -n 20 'a b.txt'", ["head", "-n", "20", "a b.txt"]),
    ("tail -n 5 x", ["tail", "-n", "5", "x"]),
    ("wc -l x", ["wc", "-l", "x"]),
    ("find . -name '*.py' -print", ["find", ".", "-name", "*.py", "-print"]),
    ("rg -n --pretty foo src", ["rg", "-n", "--pretty", "foo", "src"]),
    ("rg process.env src", ["rg", "process.env", "src"]),
    ("grep -rn TODO .", ["grep", "-rn", "TODO", "."]),
    ("tree -L 2", ["tree", "-L", "2"]),
    ("git status", ["git", "status", "--", *EXCLUDES]),
    ("git blame src/a.py", ["git", "blame", "src/a.py"]),
    ("cat config.env.ts", ["cat", "config.env.ts"]),
    ("cat env.md", ["cat", "env.md"]),
    ("ls $HOME", ["ls", "$HOME"]),
]


@pytest.mark.parametrize(("cmd", "argv"), SAFE_ALLOWED)
def test_safe_runs_allowlisted_as_argv(cmd: str, argv: list[str]) -> None:
    d = safe(cmd)
    assert d.kind == "run" and not d.blocked
    assert d.argv == argv
    assert d.shell_cmd is None


def test_custom_allowlist() -> None:
    policy = Policy(allowlist=("ls", "", "npm test"))
    assert safe("npm test", policy=policy).argv == ["npm", "test"]
    assert safe("npm install", policy=policy).blocked
    assert safe("cat x", policy=policy).blocked


def test_flag_rules_apply_to_custom_absolute_path_entry() -> None:
    policy = Policy(allowlist=("/usr/bin/find",))
    assert safe("/usr/bin/find .", policy=policy).argv == ["/usr/bin/find", "."]
    assert safe("/usr/bin/find . -delete", policy=policy).blocked


# --- Safe-mode flag denylist: one or more tests per row ------------------------------

FIND_FLAGS = ["-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
              "-fprintf", "-fls"]  # fmt: skip


@pytest.mark.parametrize("flag", FIND_FLAGS)
def test_find_row(flag: str) -> None:
    d = safe(f"find . -name x {flag} y")
    assert d.blocked and flag in d.reason


RG_BLOCKED = [
    "rg --pre sh x",
    "rg --pre=sh x",
    "rg '--pre' sh x",
    "rg --pr=sh x",
    "rg --pre-glob '*' x",
    "rg --pre-glob=* x",
    "rg --search-zip x",
    "rg --search-z x",
    "rg -z x",
    "rg -iz x",
    "rg -zi x",
    "rg --hostname-bin sh x",
]


@pytest.mark.parametrize("cmd", RG_BLOCKED)
def test_rg_row(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "rg flag" in d.reason


@pytest.mark.parametrize("cmd", ["tree -o out", "tree -ao out", "tree -R -H . x"])
def test_tree_row(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "tree flag" in d.reason


GIT_GLOBALS = [
    "git -c core.pager=sh log",
    "git -C /tmp log",
    "git --exec-path=/tmp log",
    "git --git-dir=/tmp/x log",
    "git --work-tree=/tmp log",
    "git --no-pager log",
    "git -p show",
]


@pytest.mark.parametrize("cmd", GIT_GLOBALS)
def test_git_global_options_row(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "global option" in d.reason


GIT_FLAGS = [
    "git diff --ext-diff",
    "git diff --ext-d",
    "git log -p --textconv",
    "git show --textconv HEAD",
    "git log --output=/tmp/x",
    "git log --output /tmp/x",
    "git diff --outp=/tmp/x",
    "git log --upload-pack=sh",
    "git show --open-files-in-pager=sh",
    # beyond SPEC: --full-diff shows every file of a matched commit, ignoring the excludes
    "git log -p --full-diff -- README",
    "git show --full-diff HEAD",
]


@pytest.mark.parametrize("cmd", GIT_FLAGS)
def test_git_any_row(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "git flag" in d.reason


GIT_SHOW_DENYLISTED = [
    "git show HEAD:.env",
    "git show HEAD~3:config/.env.production",
    "git show 'HEAD:.e''nv'",
    "git show HEAD:./.ENV",
    "git show :.env",
    "git show HEAD@{1}:certs/server.pem",
    "git show HEAD:keys/id_rsa",
    "git show v1:a/b/private.KEY",
]


@pytest.mark.parametrize("cmd", GIT_SHOW_DENYLISTED)
def test_git_show_rev_path_row(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "denylisted" in d.reason


@pytest.mark.parametrize(
    ("cmd", "argv"),
    [
        ("git log", ["git", "log", "--", *EXCLUDES]),
        ("git log -p HEAD~3", ["git", "log", "-p", "HEAD~3", "--", *EXCLUDES]),
        ("git diff --stat", ["git", "diff", "--stat", "--", *EXCLUDES]),
        ("git show HEAD", ["git", "show", "HEAD", "--", *EXCLUDES]),
        ("git log -- src", ["git", "log", "--", "src", *EXCLUDES]),
        ("git log -p -- '.e*'", ["git", "log", "-p", "--", ".e*", *EXCLUDES]),
        ("git diff --no-ext-diff", ["git", "diff", "--no-ext-diff", "--", *EXCLUDES]),
    ],
)
def test_git_exclude_pathspecs_row(cmd: str, argv: list[str]) -> None:
    assert safe(cmd).argv == argv


GIT_DENYLISTED_PATHSPECS = [
    "git log -- .env",
    "git diff -- 'sub/id_ed25519'",
    "git log -- ':(top).env'",
    "git log -- ':!x' ':/a.pem'",
]


@pytest.mark.parametrize("cmd", GIT_DENYLISTED_PATHSPECS)
def test_git_denylisted_pathspec_blocked(cmd: str) -> None:
    assert safe(cmd).blocked


def test_git_blame_gets_no_excludes_and_status_does() -> None:
    assert safe("git blame a.py").argv == ["git", "blame", "a.py"]
    assert safe("git status -s").argv == ["git", "status", "-s", "--", *EXCLUDES]


# --- File denylist by name (Safe) ----------------------------------------------------

SAFE_DENYLISTED_NAMES = [
    "cat .env",
    "cat ./.env",
    "cat /repo/.env",
    "cat sub/dir/.env.local",
    "cat .env.example",
    "cat .ENV",
    "cat '.e''nv'",
    'cat ".env"',
    r"cat .e\nv",
    "cat server.pem",
    "cat server.PEM",
    "head tls/private.key",
    "cat ~/.ssh/id_rsa",
    "cat id_ed25519.pub",
    "grep -f.env x",
    "grep --file=.env x",
    "grep -fid_rsa x",
    "tail -n1 a .env",
    "wc .env/x",
    "find . -name x -newer .env",
    "rg foo .env",
]


@pytest.mark.parametrize("cmd", SAFE_DENYLISTED_NAMES)
def test_safe_blocks_denylisted_names(cmd: str) -> None:
    d = safe(cmd)
    assert d.blocked and "denylisted" in d.reason


# --- Auto ----------------------------------------------------------------------------

AUTO_RUNS = [
    "npm test",
    "ls; echo hi",
    "cat a | sh",
    "echo $(whoami) > /tmp/x",
    "python3 -m pytest -q",
    "rg --pre sh x",
    "git -c x=y log",
    "echo 'unbalanced",
]


@pytest.mark.parametrize("cmd", AUTO_RUNS)
def test_auto_runs_shell_cmd(cmd: str) -> None:
    d = check(cmd, "auto", POLICY, NONE)
    assert d.kind == "run"
    assert d.shell_cmd == cmd and d.argv is None


AUTO_BLOCKED = [
    "cat .env",
    "cat .e''nv",
    'cat ".env"',
    "cat x;cat .env",
    "cat x|base64 .ENV",
    "source .env && npm test",
    "cat $(echo id_rsa)",
    "cp x.pem /tmp",
    "cat .env '",
    "git show HEAD:.env",
]


@pytest.mark.parametrize("cmd", AUTO_BLOCKED)
def test_auto_blocks_denylisted_names(cmd: str) -> None:
    d = check(cmd, "auto", POLICY, NONE)
    assert d.blocked and "denylisted" in d.reason


# --- Pre-authorization ---------------------------------------------------------------


def test_pre_authorized_exact_string_runs_via_shell() -> None:
    cmd = "cat a | sh"
    d = safe(cmd, frozenset({cmd}))
    assert d.kind == "run" and d.shell_cmd == cmd and d.argv is None
    assert "pre-authorized" in d.reason


@pytest.mark.parametrize(
    ("cmd", "approved"),
    [
        ("cat a | sh", "cat a | sh "),
        ("cat a | sh", " cat a | sh"),
        ("cat a | sh", "cat a|sh"),
        ("cat a | sh", "CAT A | SH"),
        ("rm -rf x", "rm -rf"),
        ("rm -rf x", "rm -rf x\n"),
    ],
)
def test_pre_authorization_needs_exact_match(cmd: str, approved: str) -> None:
    assert safe(cmd, frozenset({approved})).blocked


@pytest.mark.parametrize("cmd", ["cat .env", "find . -exec cat .env ;", "git show HEAD:.env"])
def test_pre_authorization_does_not_lift_file_denylist(cmd: str) -> None:
    d = safe(cmd, frozenset({cmd}))
    assert d.blocked and "denylisted" in d.reason


def test_pre_authorization_for_allowed_command_keeps_argv() -> None:
    d = safe("git log", frozenset({"git log"}))
    assert d.argv == ["git", "log", "--", *EXCLUDES] and d.shell_cmd is None


def test_pre_authorization_does_not_apply_in_read_only() -> None:
    assert check("rm -rf x", "read_only", POLICY, frozenset({"rm -rf x"})).blocked


# --- Misc ----------------------------------------------------------------------------


def test_unknown_mode_fails_closed() -> None:
    bogus: Any = "yolo"
    assert check("ls", bogus, POLICY, NONE).blocked


def test_decision_blocked_property() -> None:
    assert Decision(kind="block", reason="x").blocked
    assert not Decision(kind="run", reason="x", argv=["ls"]).blocked


def test_default_allowlist_matches_spec() -> None:
    assert Policy().allowlist == gate.DEFAULT_SAFE_ALLOWLIST == (
        "ls", "cat", "head", "tail", "wc", "find", "rg", "grep", "tree",
        "git status", "git log", "git diff", "git show", "git blame",
    )  # fmt: skip


def test_names_denylisted_path_helper() -> None:
    assert gate.names_denylisted_path("cat -x.env")
    assert not gate.names_denylisted_path("cat -x")
    assert not gate.names_denylisted_path("rg API_KEY process.env")


def test_exclude_pathspecs_are_case_insensitive() -> None:
    assert GIT_EXCLUDE_PATHSPECS == (
        ":(exclude,icase,glob)**/.env*",
        ":(exclude,icase,glob)**/.env*/**",
        ":(exclude,icase,glob)**/*.pem",
        ":(exclude,icase,glob)**/*.pem/**",
        ":(exclude,icase,glob)**/*.key",
        ":(exclude,icase,glob)**/*.key/**",
        ":(exclude,icase,glob)**/id_*",
        ":(exclude,icase,glob)**/id_*/**",
    )


@pytest.mark.parametrize("mode", ["safe", "auto"])
@pytest.mark.parametrize("cmd", ["cat .ENV", "cat x.PEM", "cat sub/ID_RSA", "cat a.Key"])
def test_upper_case_denylisted_names_blocked(cmd: str, mode: gate.Mode) -> None:
    d = check(cmd, mode, POLICY, frozenset({cmd}))
    assert d.blocked and "denylisted" in d.reason


def test_gate_uses_sandbox_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(rel_path: str) -> bool:
        seen.append(rel_path)
        return rel_path == "secret.txt"

    monkeypatch.setattr(gate, "is_denylisted", fake)
    assert safe("cat secret.txt").blocked
    assert "secret.txt" in seen
    assert safe("cat .env").argv == ["cat", ".env"]


@pytest.mark.parametrize(
    ("cmd", "reason"),
    [
        ("git", "bare git"),
        ("git grep --cached x", "not a Safe git subcommand"),
        ("git archive HEAD", "not a Safe git subcommand"),
        ("git stash show -p", "not a Safe git subcommand"),
    ],
)
def test_git_allowlisted_wholesale_still_limits_subcommands(cmd: str, reason: str) -> None:
    d = safe(cmd, policy=Policy(allowlist=("git",)))
    assert d.blocked and reason in d.reason

"""Mock GitHub: users, repos, access hooks, env guard, and the real snapshot code path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import subprocess
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ahoy.github import GitHubAuthError, RepoTarget, SnapshotError, fetch_snapshot_from
from ahoy.github.app_client import git_auth_header, remove_snapshot
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.sandbox.paths import is_denylisted

CANARY = "ghs_CANARYsnapshot0000000000000000000000"
DEMO = RepoTarget(id=7001, installation_id=INSTALLATION_ID)
PUBLIC = RepoTarget(id=7002, installation_id=INSTALLATION_ID)
BAIT = (".env", "config/dev.pem", "deploy/id_ed25519")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(  # noqa: S603
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return out.stdout


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> MockGitHub:
    monkeypatch.setenv("AHOY_ENV", "test")
    return MockGitHub(ahoy_env="test")


def sign_in(gh: MockGitHub, user: str) -> SecretStr:
    return run(gh.exchange_code(gh.issue_code(user), "v", "http://x/cb")).access_token


# --- construction guard ----------------------------------------------------------------


@pytest.mark.parametrize("env", ["prod", "staging", ""])
def test_refused_outside_dev_test(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AHOY_ENV", raising=False)
    with pytest.raises(RuntimeError, match="dev or test"):
        MockGitHub(ahoy_env=env)


def test_refused_when_process_env_is_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AHOY_ENV", "prod")
    with pytest.raises(RuntimeError):
        MockGitHub(ahoy_env="test")


@pytest.mark.parametrize("env", ["dev", "test"])
def test_allowed_in_dev_test(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AHOY_ENV", env)
    MockGitHub(ahoy_env=env)


# --- users, tokens, listing ------------------------------------------------------------


def test_sign_in_with_pkce_and_token_shapes(gh: MockGitHub) -> None:
    verifier = "x" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    code = gh.issue_code("alice", challenge.rstrip(b"=").decode())
    tokens = run(gh.exchange_code(code, verifier, "http://x/cb"))
    assert tokens.access_token.get_secret_value().startswith("ghu_")
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.get_secret_value().startswith("ghr_")
    user = run(gh.get_user(tokens.access_token))
    assert (user.login, user.id) == ("alice", 1001)
    with pytest.raises(GitHubAuthError):  # codes are single-use
        run(gh.exchange_code(code, verifier, "http://x/cb"))


def test_wrong_pkce_verifier_refused(gh: MockGitHub) -> None:
    code = gh.issue_code("bob", "some-challenge")
    with pytest.raises(GitHubAuthError):
        run(gh.exchange_code(code, "wrong", "http://x/cb"))


def test_refresh_rotates_single_use(gh: MockGitHub) -> None:
    tokens = run(gh.exchange_code(gh.issue_code("bob"), "v", "http://x/cb"))
    assert tokens.refresh_token is not None
    new = run(gh.refresh(tokens.refresh_token))
    assert run(gh.get_user(new.access_token)).login == "bob"
    with pytest.raises(GitHubAuthError):
        run(gh.refresh(tokens.refresh_token))


def test_revoke_grant_kills_tokens(gh: MockGitHub) -> None:
    tok = sign_in(gh, "alice")
    run(gh.revoke_grant(tok))
    assert run(gh.can_read_repo(tok, DEMO.id)) is False


def test_repo_access_matrix(gh: MockGitHub) -> None:
    alice, bob, mallory = (sign_in(gh, u) for u in ("alice", "bob", "mallory"))
    assert run(gh.can_read_repo(alice, DEMO.id)) is True
    assert run(gh.can_read_repo(bob, DEMO.id)) is True
    assert run(gh.can_read_repo(mallory, DEMO.id)) is False
    assert run(gh.can_read_repo(mallory, PUBLIC.id)) is True
    assert run(gh.can_read_repo(SecretStr("ghu_bogus"), PUBLIC.id)) is False
    assert [i.id for i in run(gh.list_installations(alice))] == [INSTALLATION_ID]
    assert run(gh.list_installations(mallory)) == []
    repos = run(gh.list_installation_repos(alice, INSTALLATION_ID))
    assert {(r.full_name, r.private) for r in repos} == {
        ("fixture/demo", True),
        ("fixture/public", False),
    }


def test_revoke_access_hook(gh: MockGitHub) -> None:
    bob = sign_in(gh, "bob")
    gh.revoke_access("bob", "fixture/demo")
    assert run(gh.can_read_repo(bob, DEMO.id)) is False
    assert DEMO.id not in {r.id for r in run(gh.list_installation_repos(bob, INSTALLATION_ID))}
    assert run(gh.can_read_repo(sign_in(gh, "alice"), DEMO.id)) is True
    gh.restore_access("bob", DEMO.id)
    assert run(gh.can_read_repo(bob, DEMO.id)) is True


def test_remove_from_installation_hook(gh: MockGitHub) -> None:
    alice = sign_in(gh, "alice")
    assert run(gh.can_mint_for(INSTALLATION_ID, DEMO.id)) is True
    gh.remove_from_installation("fixture/demo")
    assert run(gh.can_mint_for(INSTALLATION_ID, DEMO.id)) is False
    assert run(gh.can_read_repo(alice, DEMO.id)) is False  # private: App no longer sees it
    gh.remove_from_installation("fixture/public")
    assert run(gh.can_read_repo(alice, PUBLIC.id)) is True  # public stays readable
    assert run(gh.can_mint_for(INSTALLATION_ID, PUBLIC.id)) is False
    with pytest.raises(Exception, match="installation"):
        run(gh.resolve_ref(DEMO, None))
    gh.add_to_installation(DEMO.id)
    assert run(gh.can_mint_for(INSTALLATION_ID, DEMO.id)) is True


def test_can_mint_for_wrong_installation(gh: MockGitHub) -> None:
    assert run(gh.can_mint_for(999, DEMO.id)) is False


def test_resolve_ref_and_tree(gh: MockGitHub) -> None:
    sha = run(gh.resolve_ref(DEMO, None))
    assert run(gh.resolve_ref(DEMO, "main")) == sha
    assert run(gh.resolve_ref(DEMO, sha)) == sha
    tree = run(gh.tree(DEMO, sha))
    assert tree.truncated is False
    paths = {e.path for e in tree.entries}
    assert {"README.md", ".env", "config/dev.pem"} <= paths
    with pytest.raises(Exception, match="not found"):
        run(gh.resolve_ref(DEMO, "no-such-branch"))


# --- snapshot --------------------------------------------------------------------------


@pytest.fixture
def snap(tmp_path: Path) -> Iterator[Path]:
    dest = tmp_path / "snap"
    yield dest
    remove_snapshot(dest)


def _assert_clean_snapshot(dest: Path, sha: str) -> None:
    assert git(dest, "rev-parse", "HEAD").strip() == sha
    for rel in BAIT:
        assert not (dest / rel).exists(), rel
    for path in dest.rglob("*"):
        rel = path.relative_to(dest).as_posix()
        if not rel.startswith(".git/") and rel != ".git":
            assert not is_denylisted(rel), rel
    assert git(dest, "status", "--porcelain") == ""
    assert git(dest, "diff") == ""
    assert git(dest, "diff", "HEAD") == ""
    with pytest.raises(PermissionError):
        (dest / "README.md").open("a")
    with pytest.raises(PermissionError):
        (dest / "new-file").write_text("x")
    with pytest.raises(PermissionError):
        (dest / ".git" / "config").open("a")


def test_mock_fetch_snapshot_strips_and_locks(gh: MockGitHub, snap: Path) -> None:
    sha = run(gh.resolve_ref(DEMO, None))
    run(gh.fetch_snapshot(DEMO, sha, snap, 20))
    _assert_clean_snapshot(snap, sha)
    assert (snap / "README.md").is_file()
    assert (snap / "config").is_dir()  # only the denylisted file inside is gone


def test_snapshot_never_stores_token(gh: MockGitHub, snap: Path) -> None:
    sha = run(gh.resolve_ref(DEMO, None))
    run(fetch_snapshot_from(gh.bare_repo(DEMO.id).as_uri(), sha, snap, 3, SecretStr(CANARY)))
    _assert_clean_snapshot(snap, sha)
    config = (snap / ".git" / "config").read_text()
    assert CANARY not in config and "extraheader" not in config.lower()
    assert "url" not in config  # no remote recorded at all
    for path in (snap / ".git").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert CANARY.encode() not in data, path
            assert git_auth_header(SecretStr(CANARY)).split()[1].encode() not in data, path
    assert len(git(snap, "log", "--format=%H").split()) <= 3


def test_snapshot_of_older_commit(gh: MockGitHub, snap: Path) -> None:
    head = run(gh.resolve_ref(DEMO, None))
    older = git(gh.bare_repo(DEMO.id), "rev-parse", f"{head}~2").strip()
    run(gh.fetch_snapshot(DEMO, older, snap, 1))
    assert git(snap, "rev-parse", "HEAD").strip() == older
    assert not (snap / ".env").exists()


def test_snapshot_refused_when_repo_removed(gh: MockGitHub, snap: Path) -> None:
    sha = run(gh.resolve_ref(PUBLIC, None))
    gh.remove_from_installation(PUBLIC.id)
    with pytest.raises(Exception, match="installation"):
        run(gh.fetch_snapshot(PUBLIC, sha, snap, 1))
    assert not snap.exists()


def test_snapshot_failure_cleans_up_and_hides_token(gh: MockGitHub, snap: Path) -> None:
    missing = "0" * 40
    with pytest.raises(SnapshotError) as exc:
        run(
            fetch_snapshot_from(gh.bare_repo(DEMO.id).as_uri(), missing, snap, 1, SecretStr(CANARY))
        )
    assert CANARY not in str(exc.value)
    assert not snap.exists()


@pytest.mark.parametrize("sha", ["main", "-c", "a" * 39])
def test_snapshot_requires_full_sha(snap: Path, sha: str) -> None:
    with pytest.raises(Exception, match="SHA"):
        run(fetch_snapshot_from("file:///nonexistent", sha, snap, 1, None))


def test_snapshot_refuses_nonempty_dest(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("x")
    with pytest.raises(SnapshotError, match="not empty"):
        run(fetch_snapshot_from("file:///nonexistent", "a" * 40, tmp_path, 1, None))
    assert (tmp_path / "x").exists()


def test_git_auth_header_is_basic_x_access_token() -> None:
    scheme, value = git_auth_header(SecretStr(CANARY)).split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == f"x-access-token:{CANARY}"


def test_git_fetch_argv_carries_basic_header(
    gh: MockGitHub, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ahoy.github.app_client as app_client

    seen: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*argv: str, **kw: Any) -> asyncio.subprocess.Process:
        seen.append(argv)
        return await real(*argv, **kw)

    monkeypatch.setattr(app_client.asyncio, "create_subprocess_exec", spy)
    sha = run(gh.resolve_ref(DEMO, None))
    run(fetch_snapshot_from(gh.bare_repo(DEMO.id).as_uri(), sha, snap, 1, SecretStr(CANARY)))
    expected = f"http.extraHeader=Authorization: {git_auth_header(SecretStr(CANARY))}"
    with_token = [a for a in seen if any(CANARY in x or expected in x for x in a)]
    assert len(with_token) == 1  # only the fetch process
    assert "fetch" in with_token[0] and expected in with_token[0]
    assert not any("Bearer" in x for a in seen for x in a)


def test_set_tree_truncated_hook(gh: MockGitHub) -> None:
    sha = run(gh.resolve_ref(DEMO, None))
    gh.set_tree_truncated("fixture/demo", True)
    assert run(gh.tree(DEMO, sha)).truncated is True
    assert run(gh.tree(PUBLIC, run(gh.resolve_ref(PUBLIC, None)))).truncated is False
    gh.set_tree_truncated(DEMO.id, False)
    assert run(gh.tree(DEMO, sha)).truncated is False


def test_rename_hook_keeps_id_based_access(gh: MockGitHub, snap: Path) -> None:
    alice = sign_in(gh, "alice")
    sha = run(gh.resolve_ref(DEMO, None))
    gh.rename_repo("fixture/demo", "fixture/demo-old")
    gh.rename_repo(PUBLIC.id, "fixture/demo")  # the old name now belongs to another repo
    names = {r.id: r.full_name for r in run(gh.list_installation_repos(alice, INSTALLATION_ID))}
    assert names == {DEMO.id: "fixture/demo-old", PUBLIC.id: "fixture/demo"}
    assert run(gh.resolve_ref(DEMO, None)) == sha
    assert run(gh.can_read_repo(sign_in(gh, "mallory"), DEMO.id)) is False  # still private
    run(gh.fetch_snapshot(DEMO, sha, snap, 1))
    assert git(snap, "rev-parse", "HEAD").strip() == sha
    with pytest.raises(ValueError, match="taken"):
        gh.rename_repo(DEMO.id, "fixture/demo")

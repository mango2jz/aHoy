from pathlib import Path

import pytest

from ahoy.sandbox.paths import PathError, Snapshot, is_denylisted, resolve


def build_repo(tmp_path: Path) -> Snapshot:
    """A snapshot with planted symlinks; `outside/` holds files that must never be reached."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET outside\n")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("print('hi')\n")
    (repo / "deploy").mkdir()
    (repo / "deploy" / "id_rsa").write_text("TOPSECRET key\n")
    (repo / "certs").mkdir()
    (repo / "certs" / "x.PEM").write_text("TOPSECRET pem\n")
    (repo / ".env.local").write_text("TOPSECRET=1\n")
    (repo / "inner_link_out").symlink_to(outside / "secret.txt")
    (repo / "etc_dir").symlink_to("/etc")
    (repo / "chain2").symlink_to(outside)
    (repo / "chain1").symlink_to("chain2")
    (repo / "src" / "deeplink").symlink_to("../../outside")
    (repo / "src" / "up").symlink_to("..")
    (repo / "loop").symlink_to("loop")
    (repo / "alias_env").symlink_to(".env.local")
    (repo / "inner_ok").symlink_to("src/main.py")
    (repo / "gitlink").symlink_to(".git")
    return Snapshot(repo)


# (path, expected reason). The same table drives the tool tests in test_tools_fs.py.
ESCAPES: list[tuple[str, str]] = [
    ("inner_link_out", "symlink_escape"),
    ("etc_dir/passwd", "symlink_escape"),
    ("etc_dir", "symlink_escape"),
    ("chain1/secret.txt", "symlink_escape"),
    ("chain1", "symlink_escape"),
    ("src/deeplink/secret.txt", "symlink_escape"),
    ("src/up/../outside/secret.txt", "escape"),
    ("a/../../x", "escape"),
    ("../outside/secret.txt", "escape"),
    ("..", "escape"),
    ("/etc/passwd", "absolute"),
    ("\\etc\\passwd", "absolute"),
    (".env.local", "denylisted"),
    ("src/.ENV", "denylisted"),
    ("deploy/id_rsa", "denylisted"),
    ("certs/x.PEM", "denylisted"),
    ("certs/server.key", "denylisted"),
    ("alias_env", "denylisted"),
    ("src/main.py\x00.txt", "nul"),
    ("loop", "invalid"),
    (".git/config", "git_dir"),
    ("src/.GIT/HEAD", "git_dir"),
    (".git", "git_dir"),
    ("gitlink", "git_dir"),
    ("src/\udcff.py", "invalid"),  # lone surrogate: not UTF-8
    ("a" * 300, "invalid"),  # ENAMETOOLONG must surface as PathError, not OSError
]

ALLOWED: list[tuple[str, str]] = [
    (".", ""),
    ("", ""),
    ("src/main.py", "src/main.py"),
    ("./src//main.py", "src/main.py"),
    ("a/../src/main.py", "src/main.py"),
    ("src/up/src/main.py", "src/main.py"),
    ("inner_ok", "src/main.py"),
    ("src/missing.py", "src/missing.py"),
    (".gitignore", ".gitignore"),
    ("src/.github/x.yml", "src/.github/x.yml"),
]


@pytest.fixture
def snap(tmp_path: Path) -> Snapshot:
    return build_repo(tmp_path)


@pytest.mark.parametrize(("path", "reason"), ESCAPES)
def test_resolve_refuses(snap: Snapshot, path: str, reason: str) -> None:
    with pytest.raises(PathError) as exc:
        resolve(snap, path)
    assert exc.value.reason == reason


@pytest.mark.parametrize(("path", "rel"), ALLOWED)
def test_resolve_allows(snap: Snapshot, path: str, rel: str) -> None:
    result = resolve(snap, path)
    assert result == snap.root / rel if rel else result == snap.root
    assert result.is_relative_to(snap.root)


@pytest.mark.parametrize(
    ("rel", "denied"),
    [
        (".env", True),
        (".env.production", True),
        ("a/b/.envrc", True),
        ("x.pem", True),
        ("certs/X.Pem", True),
        ("tls.key", True),
        ("deploy/id_rsa", True),
        ("deploy/id_ed25519.pub", True),
        ("id_dir/readme.md", True),
        ("src/main.py", False),
        ("environment.py", False),
        ("keys/readme.md", False),
        ("monkey.keyboard", False),
        ("my_id_rsa", False),
    ],
)
def test_is_denylisted(rel: str, denied: bool) -> None:
    assert is_denylisted(rel) is denied


def test_snapshot_root_is_resolved(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    snap = Snapshot(tmp_path / "link")
    assert snap.root == (tmp_path / "real").resolve()
    with pytest.raises(FileNotFoundError):
        Snapshot(tmp_path / "missing")

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from ahoy.sandbox.paths import PathError, Snapshot
from ahoy.tools import ToolError, search
from ahoy.tools.fs import (
    LINE_CLIP_MARKER,
    MAX_LINE_CHARS,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    NOTE_EMPTY_DIR,
    NOTE_LFS,
    NOTE_SUBMODULE,
    list_dir,
    read_file,
)
from test_paths import ESCAPES, build_repo

LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
    "size 12345\n"
)


@pytest.fixture
def snap(tmp_path: Path) -> Snapshot:
    s = build_repo(tmp_path)
    r = s.root
    (r / ".git").mkdir()
    (r / ".git" / "config").write_text("needle in git\n")
    (r / ".gitignore").write_text("build/\n*.log\n")
    (r / "build").mkdir()
    (r / "build" / "out.txt").write_text("needle in build\n")
    (r / "debug.log").write_text("needle in log\n")
    (r / "src" / ".gitignore").write_text("generated.py\n")
    (r / "src" / "generated.py").write_text("needle generated\n")
    (r / "src" / "pkg" / "deep" / "deeper").mkdir(parents=True)
    (r / "src" / "pkg" / "deep" / "deeper" / "x.py").write_text("needle deep\n")
    (r / "src" / "app.py").write_text("".join(f"line {i} needle\n" for i in range(1, 1201)))
    (r / ".gitmodules").write_text('[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = x\n')
    (r / "vendor" / "lib").mkdir(parents=True)
    (r / "empty").mkdir()
    (r / "assets").mkdir()
    (r / "assets" / "logo.png").write_text(LFS_POINTER)
    (r / "bin.dat").write_bytes(b"needle\x00\x01")
    return s


@pytest.fixture(params=["python", "rg"])
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    if request.param == "python":
        monkeypatch.setattr(search, "find_rg", lambda: None)
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    yield request.param


# --- every tool refuses the same set --------------------------------------------------


@pytest.mark.parametrize(("path", "reason"), ESCAPES)
def test_read_file_refuses(snap: Snapshot, path: str, reason: str) -> None:
    with pytest.raises(PathError) as exc:
        read_file(snap, path)
    assert exc.value.reason == reason


@pytest.mark.parametrize(("path", "reason"), ESCAPES)
def test_list_dir_refuses(snap: Snapshot, path: str, reason: str) -> None:
    with pytest.raises(PathError) as exc:
        list_dir(snap, path)
    assert exc.value.reason == reason


def test_search_never_reaches_outside_or_denylisted(snap: Snapshot, engine: str) -> None:
    result = search.search(snap, "TOPSECRET")
    assert result.engine == engine
    assert result.hits == []
    # A user glob cannot re-include denylisted files.
    for glob in ["*.PEM", "**/id_*", ".env*", "*"]:
        assert search.search(snap, "TOPSECRET", glob=glob).hits == []


# --- list_dir -------------------------------------------------------------------------


def test_list_dir_hides_ignored_denylisted_and_git(snap: Snapshot) -> None:
    paths = {e.path for e in list_dir(snap, ".", depth=3).entries}
    assert "src/main.py" in paths
    assert "src/pkg/deep" in paths
    assert "src/pkg/deep/deeper" not in paths  # depth 3 from the root
    for hidden in [
        ".git",
        ".git/config",
        "build",
        "debug.log",
        "src/generated.py",
        ".env.local",
        "deploy/id_rsa",
        "certs/x.PEM",
    ]:
        assert hidden not in paths


def test_list_dir_does_not_follow_symlinks(snap: Snapshot) -> None:
    entries = {e.path: e for e in list_dir(snap, ".", depth=3).entries}
    assert entries["etc_dir"].kind == "symlink"
    assert entries["chain1"].kind == "symlink"
    assert not any(p.startswith(("etc_dir/", "chain1/", "src/up/")) for p in entries)


def test_list_dir_depth_clamped_and_subdir(snap: Snapshot) -> None:
    result = list_dir(snap, "src", depth=99)
    assert result.depth == 3
    assert result.path == "src"
    paths = {e.path for e in result.entries}
    assert "src/pkg/deep/deeper/x.py" not in paths
    assert "src/pkg/deep/deeper" in paths
    assert "src/generated.py" not in paths  # nested .gitignore applies from a subdir too
    assert {e.path for e in list_dir(snap, "src", depth=0).entries} >= {"src/main.py"}


def test_list_dir_notes_submodules_and_lfs(snap: Snapshot) -> None:
    entries = {e.path: e for e in list_dir(snap, ".", depth=2).entries}
    assert entries["vendor/lib"].note == NOTE_SUBMODULE
    assert entries["empty"].note == NOTE_EMPTY_DIR
    assert entries["assets/logo.png"].note == NOTE_LFS
    assert entries["src/main.py"].note is None
    assert entries["src/main.py"].size == len("print('hi')\n")


def test_list_dir_rejects_file(snap: Snapshot) -> None:
    with pytest.raises(ToolError):
        list_dir(snap, "src/main.py")


# --- read_file ------------------------------------------------------------------------


def test_read_file_pages_500_lines(snap: Snapshot) -> None:
    first = read_file(snap, "src/app.py")
    assert (first.start_line, first.end_line, first.total_lines) == (1, MAX_READ_LINES, 1200)
    assert first.truncated is True
    assert first.content.splitlines()[0] == "line 1 needle"
    assert len(first.content.splitlines()) == MAX_READ_LINES

    last = read_file(snap, "src/app.py", start_line=1001)
    assert (last.start_line, last.end_line, last.total_lines) == (1001, 1200, 1200)
    assert last.truncated is False
    assert last.content.splitlines()[-1] == "line 1200 needle"


def test_read_file_through_inner_symlink(snap: Snapshot) -> None:
    result = read_file(snap, "inner_ok")
    assert result.path == "src/main.py"
    assert result.content == "print('hi')\n"


@pytest.mark.parametrize(
    ("path", "start"),
    [
        ("src/app.py", 1201),
        ("src/app.py", 0),
        ("src", 1),
        ("bin.dat", 1),
        ("nope.py", 1),
        ("src/main.py/x", 1),
    ],
)
def test_read_file_errors(snap: Snapshot, path: str, start: int) -> None:
    with pytest.raises(ToolError):
        read_file(snap, path, start_line=start)


def test_read_file_lfs_note(snap: Snapshot) -> None:
    assert read_file(snap, "assets/logo.png").note == NOTE_LFS


# --- search ---------------------------------------------------------------------------


def test_search_caps_hits_and_skips_ignored(snap: Snapshot, engine: str) -> None:
    result = search.search(snap, "needle")
    assert result.engine == engine
    assert len(result.hits) == search.MAX_HITS
    assert result.truncated is True
    paths = {h.path for h in result.hits}
    assert not paths & {".git/config", "build/out.txt", "debug.log", "src/generated.py"}
    assert "bin.dat" not in paths


def test_search_glob_and_line_numbers(snap: Snapshot, engine: str) -> None:
    result = search.search(snap, "needle deep", glob="*.py")
    assert result.truncated is False
    assert [(h.path, h.line, h.text) for h in result.hits] == [
        ("src/pkg/deep/deeper/x.py", 1, "needle deep")
    ]
    assert search.search(snap, "line 7 needle", glob="src/app.py").hits[0].line == 7
    assert search.search(snap, "needle", glob="*.md").hits == []


def test_search_literal_query(snap: Snapshot, engine: str) -> None:
    (snap.root / "re.txt").write_text("a.b\naxb\n")
    assert [h.line for h in search.search(snap, "a.b").hits] == [1]


@pytest.mark.parametrize("query", ["", "a\x00b", "a\nb"])
def test_search_rejects_bad_query(snap: Snapshot, query: str) -> None:
    with pytest.raises(ToolError):
        search.search(snap, query)


# --- read_file byte and line caps -----------------------------------------------------


def test_read_file_huge_single_line(snap: Snapshot) -> None:
    (snap.root / "min.js").write_text("x" * (2 * 1024 * 1024))
    result = read_file(snap, "min.js")
    assert (result.start_line, result.end_line, result.total_lines) == (1, 1, 1)
    assert result.clipped_lines == 1
    assert result.content == "x" * MAX_LINE_CHARS + LINE_CLIP_MARKER
    assert result.truncated is False


def test_read_file_many_long_lines_byte_cap(snap: Snapshot) -> None:
    (snap.root / "wide.txt").write_text("".join(f"{i:05d}" + "y" * 995 + "\n" for i in range(400)))
    result = read_file(snap, "wide.txt")
    returned = result.content.splitlines()
    assert len(result.content.encode()) <= MAX_READ_BYTES
    assert result.content.endswith("\n")  # cut at a line boundary
    assert result.end_line == len(returned) < 400
    assert result.truncated is True
    assert result.clipped_lines == 0
    nxt = read_file(snap, "wide.txt", start_line=result.end_line + 1)
    assert nxt.content.startswith(f"{result.end_line:05d}")

    (snap.root / "wider.txt").write_text("z" * 3000 + "\n" + "short\n" * 3)
    clipped = read_file(snap, "wider.txt")
    assert clipped.content.splitlines() == ["z" * MAX_LINE_CHARS + LINE_CLIP_MARKER] + ["short"] * 3
    assert (clipped.end_line, clipped.total_lines, clipped.clipped_lines) == (4, 4, 1)


# --- awkward file names ---------------------------------------------------------------


def test_search_newline_in_filename(snap: Snapshot, engine: str) -> None:
    try:
        (snap.root / "we\nird.txt").write_text("needle-nl here\n")
    except OSError:
        pytest.skip("filesystem refuses newlines in names")
    (snap.root / "ird.txt").write_text("nothing\n")
    hits = search.search(snap, "needle-nl").hits
    assert [(h.path, h.line, h.text) for h in hits] == [("we\nird.txt", 1, "needle-nl here")]


def test_non_utf8_names_are_skipped(snap: Snapshot, engine: str) -> None:
    raw = os.path.join(os.fsencode(snap.root), b"bad\xff.txt")
    try:
        with open(raw, "wb") as f:
            f.write(b"needle-bad\n")
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 names")
    listing = list_dir(snap, ".")
    assert all("bad" not in e.path for e in listing.entries)
    assert listing.note is not None
    result = search.search(snap, "needle-bad")
    assert result.hits == []
    assert result.note is not None


# --- binary files with a late NUL -----------------------------------------------------


def _plant_late_nul(snap: Snapshot) -> None:
    (snap.root / "a_bin.txt").write_bytes(b"needle-bin first\n" + b"x" * 70_000 + b"\x00tail\n")
    (snap.root / "b_later.txt").write_text("needle-bin later\n")


def test_search_late_nul_binary_does_not_eat_next_hit(snap: Snapshot, engine: str) -> None:
    _plant_late_nul(snap)
    result = search.search(snap, "needle-bin")
    assert result.engine == engine
    assert [(h.path, h.line, h.text) for h in result.hits] == [
        ("b_later.txt", 1, "needle-bin later")
    ]


def test_read_file_refuses_late_nul(snap: Snapshot) -> None:
    _plant_late_nul(snap)
    with pytest.raises(ToolError, match="binary"):
        read_file(snap, "a_bin.txt")


def test_read_file_clipped_crlf_keeps_cr(snap: Snapshot) -> None:
    (snap.root / "crlf.txt").write_bytes(b"w" * 9000 + b"\r\nok\r\n")
    result = read_file(snap, "crlf.txt")
    assert result.content == "w" * MAX_LINE_CHARS + LINE_CLIP_MARKER + "\r\nok\r\n"
    assert (result.total_lines, result.clipped_lines) == (2, 1)


def test_rg_match_drops_non_utf8_line_and_path() -> None:
    from ahoy.tools.fs import WalkStats

    stats = WalkStats()
    line_bytes = (
        b'{"type":"match","data":{"path":{"text":"./c.txt"},'
        b'"lines":{"bytes":"bmVlZGxlIGNhZukK"},"line_number":1}}'
    )
    path_bytes = (
        b'{"type":"match","data":{"path":{"bytes":"Yy7pLnR4dA=="},'
        b'"lines":{"text":"needle\\n"},"line_number":1}}'
    )
    assert search._rg_match(line_bytes, stats) is None
    assert search._rg_match(path_bytes, stats) is None
    assert search._rg_match(b'{"type":"summary","data":{}}', stats) is None
    assert stats.non_utf8_skipped == 1

"""Build a deterministic git repo from the vendored fixture.

Usage: python make_fixture_git.py DEST

Copies ``fixture/`` into DEST (must not exist or be empty) and creates six
commits with a fixed author, committer and dates, so the HEAD SHA is the same on
every run. The planted denylist bait (.env, config/dev.pem, deploy/id_ed25519)
is not stored under ``fixture/`` (the repo's own ignore rules and secret
scanners would fight it); this script writes it into DEST and commits it on
purpose: it tests snapshot stripping. Only DEST is touched; the surrounding
repository's git is never used.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixture"

# Denylisted fake secrets, written into DEST and committed in the first commit.
BAIT: dict[str, bytes] = {
    ".env": (
        b"FAKE_FIXTURE_SECRET=canary-ahoy-fixture-0001\n"
        b"FAKE_API_KEY=FAKE_not_a_real_key\n"
        b"FAKE_DB_URL=postgres://fake:fake@localhost:5432/fake\n"
    ),
    "config/dev.pem": (
        b"-----BEGIN FAKE PRIVATE KEY-----\n"
        b"FAKE_PEM_CANARY_ahoy_fixture_0002\n"
        b"-----END FAKE PRIVATE KEY-----\n"
    ),
    "deploy/id_ed25519": (
        b"-----BEGIN FAKE OPENSSH PRIVATE KEY-----\n"
        b"FAKE_SSH_CANARY_ahoy_fixture_0003\n"
        b"-----END FAKE OPENSSH PRIVATE KEY-----\n"
    ),
}

# (message, date, path prefixes or exact paths); the last commit takes the rest.
COMMITS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "Scaffold project with config and deploy files",
        "2026-01-05T09:00:00+00:00",
        (
            "package.json",
            "tsconfig.json",
            ".gitignore",
            "README.md",
            ".env",
            "config/",
            "deploy/",
            "test-support/",
        ),
    ),
    (
        "Add text and scenario helpers",
        "2026-01-12T09:00:00+00:00",
        ("src/lib/", "src/libs/", "src/utils/speechMetrics.ts", "src/utils/syllables.ts"),
    ),
    (
        "Add browser audio utilities",
        "2026-01-19T09:00:00+00:00",
        ("src/utils/audio/",),
    ),
    (
        "Add flow state and end gate logic",
        "2026-01-26T09:00:00+00:00",
        (
            "src/utils/flowState",
            "src/utils/endGate",
            "src/utils/closingDetector",
            "src/utils/sessionLifecycle",
        ),
    ),
    (
        "Add live session hooks",
        "2026-02-02T09:00:00+00:00",
        ("src/hooks/",),
    ),
    (
        "Add remaining utilities",
        "2026-02-09T09:00:00+00:00",
        ("",),
    ),
]


def run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout  # noqa: S603


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dest = Path(sys.argv[1]).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"refusing: {dest} is not empty", file=sys.stderr)
        return 1
    files = sorted(p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file())
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((FIXTURE / rel).read_bytes())
        target.chmod(0o644)
    for rel, content in BAIT.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o644)
    files = sorted({*files, *BAIT})

    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(dest),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture Author",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture Author",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    run(["git", "init", "-q", "-b", "main"], dest, base)
    remaining = list(files)
    for message, date, prefixes in COMMITS:
        chosen = [f for f in remaining if any(f.startswith(p) for p in prefixes)]
        if not chosen:
            continue
        remaining = [f for f in remaining if f not in chosen]
        env = {**base, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        run(["git", "add", "--", *chosen], dest, env)
        run(["git", "commit", "-q", "--no-gpg-sign", "-m", message], dest, env)
    print(run(["git", "rev-parse", "HEAD"], dest, base).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

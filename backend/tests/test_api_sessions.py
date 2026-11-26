"""T23b: POST/GET /api/sessions and background preparation (SPEC -> Creating a session).

Runs against mock GitHub (fixture/demo 7001 private, fixture/public 7002 public). The
background preparation is awaited through `app.state.preparer.tasks`, never slept on.
"""

import asyncio
import shutil
import subprocess
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.api import sessions as api_sessions
from ahoy.api.sessions import Preparer, is_source_file, tree_refusal
from ahoy.github.app_client import RepoTarget, SnapshotError, Tree, TreeEntry, _git_env
from ahoy.github.http import GitHubHTTPError
from ahoy.github.mock import MockGitHub
from ahoy.harness.schemas import User
from ahoy.store import auth as auth_store
from ahoy.store import repo
from conftest import MakeSession
from test_api_repos import CSRF, gh_of, make_api_app, signed_in

pytestmark = pytest.mark.db

DEMO = 7001  # private
PUBLIC = 7002


@pytest.fixture
def app(db: AsyncSession, tmp_path: Path) -> FastAPI:
    return make_api_app(db, tmp_path / "cache")


async def settle(app: FastAPI) -> None:
    prep: Preparer = app.state.preparer
    while prep.tasks:
        await asyncio.gather(*list(prep.tasks))
    await app.state.runner.drain()  # the Overview step that `ready` started


async def user_of(db: AsyncSession, client: httpx.AsyncClient) -> User:
    user = await auth_store.get_user(db, UUID((await client.get("/api/me")).json()["id"]))
    assert user is not None
    return user


def bare_git(gh: MockGitHub, *args: str) -> str:
    git = shutil.which("git") or "git"
    with tempfile.TemporaryDirectory() as home:
        env = _git_env(home) | {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        out = subprocess.run(  # noqa: S603
            [git, *args], cwd=gh.bare_repo(PUBLIC), env=env, check=True, capture_output=True
        )
    return out.stdout.decode().strip()


def synthetic(paths: list[str], size: int = 10) -> Tree:
    return Tree(
        sha="a" * 40,
        truncated=False,
        entries=[TreeEntry(path=p, type="blob", size=size, mode="100644") for p in paths],
    )


def use_tree(gh: MockGitHub, tree: Tree) -> None:
    async def fake(target: RepoTarget, sha: str) -> Tree:
        return tree

    gh.tree = fake  # type: ignore[method-assign]


async def create(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post("/api/sessions", json=body, headers=CSRF)


# --- creation ------------------------------------------------------------------------


async def test_public_repo_preparing_then_ready(app: FastAPI, db: AsyncSession) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        resp = await create(alice, repo_id=PUBLIC)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "preparing" and body["status_reason"] is None
        assert body["repo_full_name"] == "fixture/public" and body["ref"] == "main"
        assert body["owner"] == "alice" and body["private"] is False and body["shared"] is False
        assert len(body["commit_sha"]) == 40
        assert "installation_id" not in body
        head = bare_git(gh_of(app), "rev-parse", "main")
        assert body["commit_sha"] == head
        await settle(app)
        got = (await alice.get(f"/api/sessions/{body['id']}")).json()
        assert got["status"] == "ready" and got["status_reason"] is None
        cache = app.state.repo_cache
        assert cache.size_of(PUBLIC, head) > 0
        assert cache.leases_of(PUBLIC, head) == 0  # the lease is only held while preparing
        root = (await repo.load_nodes(db, UUID(body["id"])))[0]
        assert root.kind == "root" and root.model == "mock"


async def test_create_needs_csrf(app: FastAPI) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        for headers in ({}, {"Origin": CSRF["Origin"]}, {"X-Ahoy-CSRF": "1"}):
            resp = await alice.post("/api/sessions", json={"repo_id": PUBLIC}, headers=headers)
            assert resp.status_code == 403


async def test_private_needs_notice_ack(app: FastAPI, db: AsyncSession) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        resp = await create(alice, repo_id=DEMO)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "private_repo_notice_required"
        user = await user_of(db, alice)
        assert await repo.list_owned_sessions(db, user.id) == []
        await alice.post("/api/me/private-repo-notice", headers=CSRF)
        resp = await create(alice, repo_id=DEMO)
        assert resp.status_code == 201 and resp.json()["private"] is True
        await settle(app)
        assert (await alice.get(f"/api/sessions/{resp.json()['id']}")).json()["status"] == "ready"


async def test_repo_not_listed_is_404(app: FastAPI) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        resp = await create(alice, repo_id=999_999)
        assert resp.status_code == 404 and resp.json()["detail"] == "repo_not_found"
        mallory = await signed_in(stack, app, "mallory")
        assert (await create(mallory, repo_id=DEMO)).status_code == 404


async def test_access_revoked_is_404(app: FastAPI) -> None:
    gh = gh_of(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        await alice.get("/api/repos")  # the listing is cached with the repo in it
        gh.remove_from_installation(PUBLIC)  # the installation can no longer mint
        resp = await create(alice, repo_id=PUBLIC)
        assert resp.status_code == 404 and resp.json()["detail"] == "repo_not_found"


async def test_create_503_when_github_down(app: FastAPI) -> None:
    gh = gh_of(app)

    async def down(*a: Any) -> Any:
        raise GitHubHTTPError("boom", status=502)

    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        gh.resolve_ref = down  # type: ignore[method-assign]
        assert (await create(alice, repo_id=PUBLIC)).status_code == 503
        gh.tree = down  # type: ignore[method-assign]
        del gh.resolve_ref
        assert (await create(alice, repo_id=PUBLIC)).status_code == 503


# --- size checks ---------------------------------------------------------------------


async def expect_422(client: httpx.AsyncClient, reason: str, **body: Any) -> dict[str, Any]:
    resp = await create(client, **({"repo_id": PUBLIC} | body))
    assert resp.status_code == 422, resp.text
    detail: dict[str, Any] = resp.json()["detail"]
    assert detail["reason"] == reason
    return detail


async def test_truncated_tree_refused(app: FastAPI, db: AsyncSession) -> None:
    gh_of(app).set_tree_truncated(PUBLIC, True)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        await expect_422(alice, "tree_truncated")
        assert await repo.list_owned_sessions(db, (await user_of(db, alice)).id) == []


async def test_too_many_source_files(app: FastAPI) -> None:
    gh = gh_of(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        use_tree(gh, synthetic([f"src/f{i}.py" for i in range(501)]))
        detail = await expect_422(alice, "too_many_files")
        assert detail["source_files"] == 501 and detail["limit"] == 500
        # vendored and binary files do not count
        ignored = [
            *(f"node_modules/x/{i}.js" for i in range(100)),
            *(f"web/vendor/{i}.go" for i in range(100)),
            *(f"img/{i}.png" for i in range(100)),
            *(f"static/app{i}.min.js" for i in range(100)),
            *(f"static/app{i}.js.map" for i in range(100)),
            "Cargo.lock",
            "fonts/a.woff2",
            "dist/bundle.js",
            "build/out.c",
        ]
        use_tree(gh, synthetic([f"src/f{i}.py" for i in range(500)] + ignored))
        assert (await create(alice, repo_id=PUBLIC)).status_code == 201
        await settle(app)


async def test_repo_too_large(app: FastAPI) -> None:
    gh = gh_of(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        # binary files still count towards the byte total
        use_tree(gh, synthetic(["a.py", "big.zip"], size=100 * 1024 * 1024 + 1))
        detail = await expect_422(alice, "repo_too_large")
        assert detail["bytes"] == 2 * (100 * 1024 * 1024 + 1)


def test_source_file_rules() -> None:
    assert is_source_file("src/app.py")
    assert is_source_file("README")
    assert is_source_file("builder/x.py")  # only whole directory names are vendored
    for path in (
        "node_modules/a.js",
        "a/node_modules/b.js",
        "vendor/x.go",
        "third_party/y.c",
        "dist/z.js",
        "build/w.o",
        ".git/config",
        "logo.PNG",
        "f.ttf",
        "a.tar.gz",
        "app.min.js",
        "app.js.map",
        "poetry.lock",
    ):
        assert not is_source_file(path), path
    tree = synthetic([f"src/f{i}.py" for i in range(400)])
    assert tree_refusal(tree) is None
    # directories and submodules are not files: 601 entries, 400 of them blobs
    tree.entries += [TreeEntry(path=f"d{i}", type="tree", mode="040000") for i in range(101)]
    tree.entries += [
        TreeEntry(path=f"m{i}", type="commit", mode="160000", size=10**9) for i in range(100)
    ]
    assert tree_refusal(tree) is None


# --- refs ----------------------------------------------------------------------------


async def test_pasted_sha_on_default_branch(app: FastAPI) -> None:
    gh = gh_of(app)
    older = bare_git(gh, "rev-parse", "main~1")
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        for ref in (older, older[:12], bare_git(gh, "rev-parse", "main")):
            resp = await create(alice, repo_id=PUBLIC, ref=ref)
            assert resp.status_code == 201, resp.text
            assert resp.json()["ref"] == ref
        assert resp.json()["commit_sha"] == bare_git(gh, "rev-parse", "main")
        await settle(app)


async def test_pasted_sha_not_on_default_branch(app: FastAPI) -> None:
    gh = gh_of(app)
    # A commit object no branch reaches (like a fork's commit GitHub would still resolve).
    stray = bare_git(gh, "commit-tree", "main^{tree}", "-p", "main", "-m", "fork-only")
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        await expect_422(alice, "ref_not_on_default_branch", ref=stray)
        await expect_422(alice, "ref_not_on_default_branch", ref=stray[:10])


async def test_branch_or_tag_off_default_branch_accepted(app: FastAPI) -> None:
    gh = gh_of(app)
    stray = bare_git(gh, "commit-tree", "main^{tree}", "-p", "main", "-m", "side")
    names = ("cafe1234", "feature/x")  # a hex-looking branch name is still a branch
    for name in names:
        bare_git(gh, "update-ref", f"refs/heads/{name}", stray)
    bare_git(gh, "update-ref", "refs/tags/v-side", stray)
    try:
        async with AsyncExitStack() as stack:
            alice = await signed_in(stack, app, "alice")
            for ref in (*names, "v-side"):
                resp = await create(alice, repo_id=PUBLIC, ref=ref)
                assert resp.status_code == 201, (ref, resp.text)
                assert resp.json()["commit_sha"] == stray and resp.json()["ref"] == ref
            await settle(app)
    finally:
        for name in names:
            bare_git(gh, "update-ref", "-d", f"refs/heads/{name}")
        bare_git(gh, "update-ref", "-d", "refs/tags/v-side")


async def test_pull_style_ref_off_default_branch_refused(app: FastAPI) -> None:
    gh = gh_of(app)
    stray = bare_git(gh, "commit-tree", "main^{tree}", "-p", "main", "-m", "pr")
    older = bare_git(gh, "rev-parse", "main~1")
    bare_git(gh, "update-ref", "refs/pull/1/head", stray)
    bare_git(gh, "update-ref", "refs/pull/2/head", older)
    try:
        async with AsyncExitStack() as stack:
            alice = await signed_in(stack, app, "alice")
            await expect_422(alice, "ref_not_on_default_branch", ref="refs/pull/1/head")
            # a pull ref whose commit is on the default branch is fine
            resp = await create(alice, repo_id=PUBLIC, ref="refs/pull/2/head")
            assert resp.status_code == 201 and resp.json()["commit_sha"] == older
            await settle(app)
    finally:
        bare_git(gh, "update-ref", "-d", "refs/pull/1/head")
        bare_git(gh, "update-ref", "-d", "refs/pull/2/head")


async def test_visibility_comes_from_github_not_the_listing(app: FastAPI) -> None:
    gh = gh_of(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        listed = (await alice.get("/api/repos")).json()["repos"]
        assert next(r for r in listed if r["id"] == PUBLIC)["private"] is False
        gh.set_private(PUBLIC, True)  # inside the 60 s listing window
        try:
            resp = await create(alice, repo_id=PUBLIC)
            assert resp.status_code == 409
            assert resp.json()["detail"] == "private_repo_notice_required"
            await alice.post("/api/me/private-repo-notice", headers=CSRF)
            resp = await create(alice, repo_id=PUBLIC)
            assert resp.status_code == 201 and resp.json()["private"] is True
            await settle(app)
        finally:
            gh.set_private(PUBLIC, False)


async def test_bad_refs(app: FastAPI) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        for ref in ("a..b", "-x", "x y", "/abs", "a//b", "a/./b", "main.lock", "main/"):
            await expect_422(alice, "bad_ref", ref=ref)
        await expect_422(alice, "ref_not_found", ref="no-such-branch")
        resp = await create(alice, repo_id=PUBLIC, ref="x" * 256)
        assert resp.status_code == 422  # body validation
        resp = await create(alice, repo_id=PUBLIC, ref="  ")
        assert resp.status_code == 201 and resp.json()["ref"] == "main"
        await settle(app)


# --- background preparation ----------------------------------------------------------


async def test_fetch_failure_sets_failed_with_safe_reason(app: FastAPI) -> None:
    gh = gh_of(app)
    secret = "ghs_LEAKcanary000000000000000000000000000"

    async def broken(target: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        await asyncio.sleep(0)
        raise SnapshotError(f"git fetch failed: https://x-access-token:{secret}@github.com/x")

    gh.fetch_snapshot = broken  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        sid = (await create(alice, repo_id=PUBLIC)).json()["id"]
        await settle(app)
        resp = await alice.get(f"/api/sessions/{sid}")
        got = resp.json()
        assert got["status"] == "failed" and got["status_reason"] == "snapshot_fetch_failed"
        assert "ghs_" not in resp.text and "github.com" not in resp.text


@pytest.mark.parametrize(
    ("error", "reason"),
    [(GitHubHTTPError("x", status=500), "github_unavailable"), (OSError("x"), "internal_error")],
)
async def test_other_failures(app: FastAPI, error: Exception, reason: str) -> None:
    async def broken(*a: Any) -> None:
        raise error

    gh_of(app).fetch_snapshot = broken  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        sid = (await create(alice, repo_id=PUBLIC)).json()["id"]
        await settle(app)
        assert (await alice.get(f"/api/sessions/{sid}")).json()["status_reason"] == reason


async def test_snapshot_oversize_fails(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_sessions, "MAX_SNAPSHOT_BYTES", 1)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        sid = (await create(alice, repo_id=PUBLIC)).json()["id"]
        await settle(app)
        got = (await alice.get(f"/api/sessions/{sid}")).json()
        assert got["status"] == "failed" and got["status_reason"] == "snapshot_too_large"


async def test_startup_marks_stale_preparing_failed(
    app: FastAPI, db: AsyncSession, sign_up: Any
) -> None:
    owner = await sign_up(app.state.auth, "alice")
    stale, _ = await repo.create_session(
        db,
        owner_id=owner.id,
        installation_id=5001,
        repo_id=PUBLIC,
        repo_full_name="fixture/public",
        ref="main",
        commit_sha="0" * 40,
        private=False,
        model="mock",
    )
    assert stale.status == "preparing"
    async with app.router.lifespan_context(app):
        got = await repo.get_session(db, stale.id)
        assert got is not None and got.status == "failed" and got.status_reason == "interrupted"


async def test_recovered_session_stays_failed_when_preparation_finishes(
    app: FastAPI, db: AsyncSession
) -> None:
    gh = gh_of(app)
    real = gh.fetch_snapshot
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(target: RepoTarget, sha: str, dest: Path, depth: int) -> None:
        started.set()
        await release.wait()
        await real(target, sha, dest, depth)

    gh.fetch_snapshot = slow  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        sid = (await create(alice, repo_id=PUBLIC)).json()["id"]
        await started.wait()
        prep: Preparer = app.state.preparer
        assert await prep.recover() == 1  # e.g. a second process starting up
        release.set()
        await settle(app)
        got = (await alice.get(f"/api/sessions/{sid}")).json()
        assert got["status"] == "failed" and got["status_reason"] == "interrupted"


async def test_shutdown_cancels_preparation(app: FastAPI) -> None:
    gh = gh_of(app)
    started = asyncio.Event()

    async def hang(*a: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    gh.fetch_snapshot = hang  # type: ignore[method-assign]
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        assert (await create(alice, repo_id=PUBLIC)).status_code == 201
        await started.wait()
        prep: Preparer = app.state.preparer
        tasks = set(prep.tasks)
        await prep.shutdown(5)
        assert all(t.done() for t in tasks) and not prep.tasks


# --- list and read -------------------------------------------------------------------


async def test_list_hides_sessions_without_access(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    gh = gh_of(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        owner = await user_of(db, alice)
        first = await make_session(owner, repo_id=DEMO)
        second = await make_session(owner, repo_id=PUBLIC)
        bob = await signed_in(stack, app, "bob")
        await make_session(await user_of(db, bob), repo_id=PUBLIC)  # not alice's
        listed = (await alice.get("/api/sessions")).json()
        assert [s["id"] for s in listed] == [str(second.id), str(first.id)]  # newest first
        gh.revoke_access("alice", DEMO)
        app.state.auth.access.invalidate(DEMO)
        listed = (await alice.get("/api/sessions")).json()
        assert [s["id"] for s in listed] == [str(second.id)]


async def test_list_503_when_github_down(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    gh = gh_of(app)

    async def down(*a: Any) -> bool:
        raise GitHubHTTPError("boom", status=502)

    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        await make_session(await user_of(db, alice), repo_id=PUBLIC)
        gh.can_read_repo = down  # type: ignore[method-assign]
        assert (await alice.get("/api/sessions")).status_code == 503


async def test_sign_in_drops_a_cached_denial(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    gh = gh_of(app)
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        session = await make_session(await user_of(db, alice))
        gh.revoke_access("alice", DEMO)
        assert (await alice.get(f"/api/sessions/{session.id}")).status_code == 404  # cached
        gh.restore_access("alice", DEMO)  # e.g. the App was just installed for her
        assert (await alice.get(f"/api/sessions/{session.id}")).status_code == 404  # still
        resp = await alice.get("/auth/mock/login", params={"user": "alice"})
        assert resp.status_code == 303
        assert (await alice.get(f"/api/sessions/{session.id}")).status_code == 200


async def test_get_session_owner_shared_stranger(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    async with AsyncExitStack() as stack:
        alice = await signed_in(stack, app, "alice")
        bob = await signed_in(stack, app, "bob")
        mallory = await signed_in(stack, app, "mallory")
        owner = await user_of(db, alice)
        shared = await make_session(owner, shared=True)
        unshared = await make_session(owner)
        got = await alice.get(f"/api/sessions/{shared.id}")
        assert got.status_code == 200 and got.json()["owner"] == "alice"
        got = await bob.get(f"/api/sessions/{shared.id}")
        assert got.status_code == 200 and got.json()["owner"] == "alice"
        assert got.json()["shared"] is True
        assert (await bob.get(f"/api/sessions/{unshared.id}")).status_code == 404
        assert (await mallory.get(f"/api/sessions/{shared.id}")).status_code == 404
        assert (await alice.get(f"/api/sessions/{UUID(int=1)}")).status_code == 404

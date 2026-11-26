"""Repo snapshot cache: single-flight fetch, leases, LRU eviction, adoption."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ahoy.github import GitHubError, RepoTarget, SnapshotError, remove_snapshot, repo_cache
from ahoy.github.app_client import _make_read_only
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.github.repo_cache import RepoCache

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DEMO = RepoTarget(id=7001, installation_id=INSTALLATION_ID)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


class FakeFetch:
    """Writes `size` bytes into the destination, counting calls; optional gate/failure."""

    def __init__(self, size: int = 100, *, read_only: bool = True) -> None:
        self.size = size
        self.read_only = read_only
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.fail_next = False
        self.error: BaseException | None = None

    async def __call__(self, dest: Path) -> None:
        self.calls += 1
        self.entered.set()
        assert dest.is_dir() and not any(dest.iterdir())
        (dest / "sub").mkdir()
        (dest / "sub" / "data.bin").write_bytes(b"x" * self.size)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next:
            self.fail_next = False
            raise self.error if self.error is not None else GitHubError("fetch failed")
        if self.read_only:
            _make_read_only(dest)


def visible(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.glob("*/*"))


async def test_lease_fetches_once_and_yields_path(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    async with cache.lease(1, SHA_A, fetch) as path:
        assert path == tmp_path / "1" / SHA_A
        assert (path / "sub" / "data.bin").read_bytes() == b"x" * 100
        assert cache.leases_of(1, SHA_A) == 1
    async with cache.lease(1, SHA_A, fetch):
        pass
    assert fetch.calls == 1
    assert cache.size_of(1, SHA_A) == 100
    assert cache.size_of(1, SHA_B) is None
    assert cache.leases_of(1, SHA_A) == 0
    assert visible(tmp_path) == [f"1/{SHA_A}"]


async def test_invalid_sha_rejected(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    with pytest.raises(GitHubError):
        async with cache.lease(1, "../etc", FakeFetch()):
            pass


async def test_concurrent_leases_share_one_fetch(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    fetch.gate = asyncio.Event()
    entered = 0
    release = asyncio.Event()

    async def one() -> Path:
        nonlocal entered
        async with cache.lease(1, SHA_A, fetch) as path:
            entered += 1
            await release.wait()
            return path

    tasks = [asyncio.create_task(one()) for _ in range(10)]
    await fetch.entered.wait()
    # Mid-fetch: the work happens in a temp sibling; nothing under the final name yet.
    assert not (tmp_path / "1" / SHA_A).exists()
    assert [p.name.startswith(".tmp-") for p in (tmp_path / "1").iterdir()] == [True]
    fetch.gate.set()
    while entered < 10:
        await asyncio.sleep(0.01)
    assert cache.leases_of(1, SHA_A) == 10
    release.set()
    paths = await asyncio.gather(*tasks)
    assert fetch.calls == 1
    assert set(paths) == {tmp_path / "1" / SHA_A}
    assert cache.leases_of(1, SHA_A) == 0


async def test_failed_fetch_reaches_all_waiters_leaves_nothing_and_retries(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    fetch.gate = asyncio.Event()
    fetch.fail_next = True

    async def one() -> None:
        async with cache.lease(1, SHA_A, fetch):
            pass

    tasks = [asyncio.create_task(one()) for _ in range(5)]
    await fetch.entered.wait()
    await asyncio.sleep(0)  # let every waiter join the fetch
    fetch.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, GitHubError) for r in results)
    assert fetch.calls == 1
    assert list((tmp_path / "1").iterdir()) == []  # no final dir, no temp dir
    assert cache.size_of(1, SHA_A) is None
    assert cache.leases_of(1, SHA_A) == 0

    fetch.gate = None
    async with cache.lease(1, SHA_A, fetch) as path:
        assert (path / "sub" / "data.bin").exists()
    assert fetch.calls == 2


async def test_cancelled_waiter_does_not_cancel_shared_fetch(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    fetch.gate = asyncio.Event()

    async def one() -> Path:
        async with cache.lease(1, SHA_A, fetch) as path:
            return path

    first = asyncio.create_task(one())
    second = asyncio.create_task(one())
    await fetch.entered.wait()
    await asyncio.sleep(0)  # let `second` join the fetch
    first.cancel()
    fetch.gate.set()
    assert await second == tmp_path / "1" / SHA_A
    with pytest.raises(asyncio.CancelledError):
        await first
    assert fetch.calls == 1
    assert cache.leases_of(1, SHA_A) == 0


async def test_lru_eviction_order(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 250, clock=Clock())
    fetch = FakeFetch(100)
    for sha in (SHA_A, SHA_B):
        async with cache.lease(1, sha, fetch):
            pass
    # Touch A: B is now least recently used.
    async with cache.lease(1, SHA_A, fetch):
        pass
    async with cache.lease(1, SHA_C, fetch):
        pass
    assert visible(tmp_path) == [f"1/{SHA_A}", f"1/{SHA_C}"]
    assert cache.total_bytes == 200
    assert fetch.calls == 3


async def test_eviction_never_removes_leased_entry(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 150, clock=Clock())
    fetch = FakeFetch(100)
    async with cache.lease(1, SHA_A, fetch):
        # A is oldest but leased; fetching B goes over budget and must not touch A.
        async with cache.lease(1, SHA_B, fetch):
            assert visible(tmp_path) == [f"1/{SHA_A}", f"1/{SHA_B}"]
            await cache.evict()  # everything leased: stays over budget, no error
            assert cache.total_bytes == 200
        # B released, A still leased: B goes.
        await cache.evict()
        assert visible(tmp_path) == [f"1/{SHA_A}"]
    assert fetch.calls == 2


async def test_evicted_entry_is_refetched(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 150, clock=Clock())
    fetch = FakeFetch(100)
    async with cache.lease(1, SHA_A, fetch):
        pass
    async with cache.lease(2, SHA_A, fetch):
        pass
    assert visible(tmp_path) == [f"2/{SHA_A}"]
    async with cache.lease(1, SHA_A, fetch) as path:
        assert (path / "sub" / "data.bin").exists()
    assert fetch.calls == 3
    assert visible(tmp_path) == [f"1/{SHA_A}"]
    assert not any(p.name.startswith(".tmp-") for p in tmp_path.glob("*/*"))


async def test_startup_adopts_and_cleans_temp(tmp_path: Path) -> None:
    def make(rel: str, size: int) -> None:
        d = tmp_path / rel
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"y" * size)
        _make_read_only(d)

    make(f"1/{SHA_A}", 30)
    make(f"2/{SHA_B}", 40)
    make(f"1/.tmp-{SHA_C}-dead", 10)
    make(f"2/.tmp-evict-{SHA_A}-dead", 10)
    old = (tmp_path / "1" / SHA_A).stat().st_mtime - 100
    os.utime(tmp_path / "1" / SHA_A, (old, old))
    (tmp_path / "notes").mkdir()  # unrelated: left alone

    cache = RepoCache(tmp_path, 1_000)
    await cache.start()
    assert visible(tmp_path) == [f"1/{SHA_A}", f"2/{SHA_B}"]
    assert cache.size_of(1, SHA_A) == 30
    assert cache.size_of(2, SHA_B) == 40
    assert (tmp_path / "notes").is_dir()

    fetch = FakeFetch()
    async with cache.lease(1, SHA_A, fetch):
        pass
    assert fetch.calls == 0

    # Adopted entries are evictable, oldest mtime first.
    cache2 = RepoCache(tmp_path, 50)
    await cache2.start()
    assert visible(tmp_path) == [f"2/{SHA_B}"]


async def test_lease_runs_startup_lazily(tmp_path: Path) -> None:
    d = tmp_path / "3" / SHA_A
    d.mkdir(parents=True)
    (d / "f").write_bytes(b"z")
    cache = RepoCache(tmp_path, 1_000)
    fetch = FakeFetch()
    async with cache.lease(3, SHA_A, fetch):
        pass
    assert fetch.calls == 0


async def test_real_snapshot_through_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AHOY_ENV", "test")
    gh = MockGitHub(ahoy_env="test")
    sha = await gh.resolve_ref(DEMO, None)
    calls = 0

    async def fetch(dest: Path) -> None:
        nonlocal calls
        calls += 1
        await gh.fetch_snapshot(DEMO, sha, dest, 1)

    cache = RepoCache(tmp_path, 1)  # tiny budget: over it whenever unleased

    async def snap() -> None:
        async with cache.lease(DEMO.id, sha, fetch) as path:
            assert (path / ".git").is_dir()
            assert not os.access(path, os.W_OK)
            size = cache.size_of(DEMO.id, sha)
            assert size is not None and size > 0

    await asyncio.gather(snap(), snap(), snap())
    assert calls == 1
    await cache.drain()  # release scheduled eviction; the read-only tree is removed
    assert list((tmp_path / str(DEMO.id)).iterdir()) == []
    await snap()
    assert calls == 2


async def test_repo_id_must_be_a_positive_int(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    for bad, exc in ((True, TypeError), ("1", TypeError), (1.0, TypeError), (0, ValueError)):
        with pytest.raises(exc):
            async with cache.lease(bad, SHA_A, FakeFetch()):  # type: ignore[arg-type]
                pass
    assert not (tmp_path / "1").exists()


async def test_exception_in_body_releases_lease(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    with pytest.raises(ValueError):
        async with cache.lease(1, SHA_A, FakeFetch()):
            raise ValueError
    assert cache.leases_of(1, SHA_A) == 0
    assert cache.size_of(1, SHA_A) == 100


async def test_fetch_fails_while_all_waiters_cancelled(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    fetch.gate = asyncio.Event()
    fetch.fail_next = True

    async def one() -> None:
        async with cache.lease(1, SHA_A, fetch):
            pass

    waiter = asyncio.create_task(one())
    await fetch.entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    fetch.gate.set()
    while (1, SHA_A) in cache._inflight:
        await asyncio.sleep(0)
    assert cache.leases_of(1, SHA_A) == 0
    assert list((tmp_path / "1").iterdir()) == []
    fetch.gate = None
    async with cache.lease(1, SHA_A, fetch):
        pass
    assert fetch.calls == 2


async def test_fetch_raising_cancelled_error_is_a_snapshot_error(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    fetch.fail_next = True
    fetch.error = asyncio.CancelledError()
    with pytest.raises(SnapshotError):
        async with cache.lease(1, SHA_A, fetch):
            pass
    assert cache.leases_of(1, SHA_A) == 0
    assert list((tmp_path / "1").iterdir()) == []


async def test_immediate_retry_after_failure_fetches_again(tmp_path: Path) -> None:
    """A lease made the instant the failed fetch task finishes (before its done-callback
    runs) must start a new fetch, not await the finished task and get its old error."""
    cache = RepoCache(tmp_path, 10_000)
    await cache.start()
    key = (1, SHA_A)
    calls = 0

    async def fetch(dest: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0)
            raise GitHubError("first failure")
        (dest / "f").write_bytes(b"ok")

    async def first() -> None:
        with pytest.raises(GitHubError, match="first failure"):
            async with cache.lease(*key, fetch):
                pass

    async def second() -> Path:
        task = None
        while task is None:
            task = cache._inflight.get(key)
            await asyncio.sleep(0)
        while not task.done():
            await asyncio.sleep(0)
        async with cache.lease(*key, fetch) as path:  # same loop step as completion
            return path

    path, _ = await asyncio.gather(second(), first())
    assert calls == 2
    assert (path / "f").read_bytes() == b"ok"


async def test_release_shrinks_over_budget_cache(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 150, clock=Clock())
    fetch = FakeFetch(100)
    async with cache.lease(1, SHA_A, fetch):
        async with cache.lease(1, SHA_B, fetch):
            assert cache.total_bytes == 200  # both leased: over budget
        # B released: background eviction removes it (A still leased).
        await cache.drain()
        assert visible(tmp_path) == [f"1/{SHA_A}"]


async def test_lease_during_eviction_delete_refetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = RepoCache(tmp_path, 150, clock=Clock())
    fetch = FakeFetch(100)
    async with cache.lease(1, SHA_A, fetch):
        pass
    deleting = asyncio.Event()
    finish = asyncio.Event()
    real_remove = repo_cache.remove_snapshot

    def slow_remove(path: Path) -> None:
        if SHA_B in path.name:
            loop.call_soon_threadsafe(deleting.set)
            asyncio.run_coroutine_threadsafe(finish.wait(), loop).result(timeout=10)
        real_remove(path)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(repo_cache, "remove_snapshot", slow_remove)
    cache._entries[(1, SHA_B)] = repo_cache._Entry(size=100, last_used=(0.5, -1))
    (tmp_path / "1" / SHA_B).mkdir()
    evicting = asyncio.create_task(cache.evict())  # B is LRU: renamed, slow delete
    await deleting.wait()
    assert not (tmp_path / "1" / SHA_B).exists()
    async with cache.lease(1, SHA_B, fetch) as path:
        assert (path / "sub" / "data.bin").exists()
    finish.set()
    await evicting
    assert fetch.calls == 2
    assert (tmp_path / "1" / SHA_B / "sub" / "data.bin").exists()


async def test_eviction_rename_failure_keeps_key_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = RepoCache(tmp_path, 150, clock=Clock())
    fetch = FakeFetch(100)
    async with cache.lease(1, SHA_A, fetch):
        pass
    real_rename = Path.rename

    def bad_rename(self: Path, target: Path | str) -> Path:
        if ".tmp-evict" in str(target):
            raise PermissionError("EPERM")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", bad_rename)
    async with cache.lease(1, SHA_B, fetch):
        pass
    await cache.evict()  # rename keeps failing: logged, no error
    monkeypatch.undo()
    assert cache.size_of(1, SHA_A) == 100
    async with cache.lease(1, SHA_A, fetch) as path:
        assert (path / "sub" / "data.bin").exists()
    assert fetch.calls == 2


async def test_unindexed_final_dir_is_adopted(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    await cache.start()
    d = tmp_path / "1" / SHA_A
    d.mkdir(parents=True)
    (d / "f").write_bytes(b"q" * 7)
    fetch = FakeFetch()
    async with cache.lease(1, SHA_A, fetch) as path:
        assert (path / "f").exists()
    assert cache.size_of(1, SHA_A) == 7
    assert visible(tmp_path) == [f"1/{SHA_A}"]


async def test_stale_index_entry_is_refetched(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path, 10_000)
    fetch = FakeFetch()
    async with cache.lease(1, SHA_A, fetch):
        pass
    remove_snapshot(tmp_path / "1" / SHA_A)
    async with cache.lease(1, SHA_A, fetch) as path:
        assert (path / "sub" / "data.bin").exists()
    assert fetch.calls == 2


async def test_drain_cancel_leaves_eviction_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = RepoCache(tmp_path, 50)
    fetch = FakeFetch(100)
    finish = asyncio.Event()
    deleting = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_remove = repo_cache.remove_snapshot

    def slow_remove(path: Path) -> None:
        loop.call_soon_threadsafe(deleting.set)
        asyncio.run_coroutine_threadsafe(finish.wait(), loop).result(timeout=10)
        real_remove(path)

    monkeypatch.setattr(repo_cache, "remove_snapshot", slow_remove)
    async with cache.lease(1, SHA_A, fetch):
        pass  # release: over budget, background eviction starts
    await deleting.wait()
    drainer = asyncio.create_task(cache.drain())
    await asyncio.sleep(0)
    drainer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drainer
    assert cache._background and not any(t.cancelled() for t in cache._background)
    finish.set()
    await cache.drain()
    assert list((tmp_path / "1").iterdir()) == []

"""The on-disk repo snapshot cache: `${root}/<repo_id>/<sha>/`, leased, LRU-evicted.

Content at a SHA never changes, so one entry serves every session on that commit. Callers
go through `authorize()` first; the cache itself knows nothing about users.

- `lease()` yields the snapshot path, fetching it first if absent. A fetch writes into a
  temp sibling (`<repo_id>/.tmp-...`) and is renamed into place, so a half-fetched
  directory is never visible under its final name.
- Concurrent leases for one key share one fetch. If it fails, every waiter gets the
  error, nothing is left on disk, and the next lease tries again.
- `evict()` removes least-recently-used, unleased entries while the total size is over
  budget. Leased entries and fetches in flight are never touched. A victim is first
  renamed to a temp name (so a new lease re-fetches cleanly) and then deleted.
- `start()` adopts existing snapshots and deletes stray temp directories (a crash mid
  fetch or mid eviction). The first `lease()` runs it if nobody did.

Single asyncio process only: there is no cross-process locking.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ahoy.github.app_client import SnapshotError, check_sha, remove_snapshot

log = logging.getLogger("ahoy.repo_cache")

TMP_PREFIX = ".tmp-"
_SHA_DIR = re.compile(r"^[0-9a-f]{40}$")
_REPO_DIR = re.compile(r"^[0-9]+$")

type Key = tuple[int, str]
type Fetch = Callable[[Path], Awaitable[None]]


@dataclass
class _Entry:
    size: int
    last_used: tuple[float, int]


def _key(repo_id: int, sha: str) -> Key:
    if isinstance(repo_id, bool) or not isinstance(repo_id, int):
        raise TypeError("repo_id must be an int")
    if repo_id < 1:
        raise ValueError("repo_id must be positive")
    return (repo_id, check_sha(sha))


def _du(path: Path) -> int:
    """Apparent size in bytes of everything under `path` (symlinks not followed)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in [*dirnames, *filenames]:
            st = os.lstat(os.path.join(dirpath, name))
            if not stat.S_ISDIR(st.st_mode):
                total += st.st_size
    return total


class RepoCache:
    def __init__(
        self, root: Path, max_bytes: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._root = Path(root)
        self._max_bytes = int(max_bytes)
        self._clock = clock
        self._seq = itertools.count()
        self._entries: dict[Key, _Entry] = {}
        self._leases: dict[Key, int] = {}
        self._inflight: dict[Key, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._started = False
        self._start_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"RepoCache(root={str(self._root)!r}, max_bytes={self._max_bytes})"

    # --- public ---------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self._entries.values())

    def path_for(self, repo_id: int, sha: str) -> Path:
        return self._path(_key(repo_id, sha))

    def size_of(self, repo_id: int, sha: str) -> int | None:
        """On-disk (apparent) size of a cached snapshot in bytes; None if not cached."""
        entry = self._entries.get(_key(repo_id, sha))
        return entry.size if entry is not None else None

    def leases_of(self, repo_id: int, sha: str) -> int:
        return self._leases.get(_key(repo_id, sha), 0)

    async def start(self) -> None:
        """Adopt existing snapshots under root and delete stray temp directories."""
        async with self._start_lock:
            if self._started:
                return
            found = await asyncio.to_thread(self._scan)
            # Oldest first, so recency follows the snapshots' age on disk.
            for _mtime, key, size in sorted(found, key=lambda f: f[0]):
                self._entries.setdefault(key, _Entry(size=size, last_used=self._now()))
            self._started = True
            if found:
                log.info(
                    "repo cache adopted %d snapshot(s), %d bytes", len(found), self.total_bytes
                )
        await self.evict()

    @contextlib.asynccontextmanager
    async def lease(self, repo_id: int, sha: str, fetch: Fetch) -> AsyncIterator[Path]:
        """Hold the snapshot for `(repo_id, sha)`; fetch it with `fetch(tmp_dest)` if absent.

        `fetch` gets an existing, empty temp directory and must fill it (it may make it
        read-only). On failure it should raise; whatever it left behind is removed.
        """
        key = _key(repo_id, sha)
        if not self._started:
            await self.start()
        self._leases[key] = self._leases.get(key, 0) + 1
        try:
            if key in self._entries and not os.path.isdir(self._path(key)):
                # Deleted behind our back: forget it and fetch again.
                log.warning("repo cache entry %s/%s vanished from disk", *key)
                del self._entries[key]
            if key not in self._entries:
                task = self._inflight.get(key)
                if task is None or task.done():
                    task = asyncio.create_task(self._fetch(key, fetch))
                    self._inflight[key] = task
                    task.add_done_callback(functools.partial(self._fetch_done, key))
                # shield: one waiter being cancelled must not cancel the shared fetch.
                await asyncio.shield(task)
            self._touch(key)
            yield self._path(key)
        finally:
            left = self._leases[key] - 1
            if left:
                self._leases[key] = left
            else:
                del self._leases[key]
                if self.total_bytes > self._max_bytes:
                    self._evict_soon()
            self._touch(key)

    async def evict(self, *, quiet: bool = False) -> None:
        """While over budget, remove the least-recently-used unleased entries.

        `quiet` logs "nothing can be evicted" at debug (release-triggered runs, which
        would otherwise warn on every release while everything is leased).
        """
        victims: list[Path] = []
        skipped: set[Key] = set()
        total = self.total_bytes
        while total > self._max_bytes:
            idle = [k for k in self._entries if self._leases.get(k, 0) == 0 and k not in skipped]
            if not idle:
                log.log(
                    logging.DEBUG if quiet else logging.WARNING,
                    "repo cache over budget (%d > %d bytes) but nothing can be evicted",
                    total,
                    self._max_bytes,
                )
                break
            key = min(idle, key=lambda k: self._entries[k].last_used)
            # Rename synchronously (one syscall) so the final path is free at once: a
            # lease arriving during the slow delete re-fetches instead of racing it.
            final = self._path(key)
            trash = final.with_name(f"{TMP_PREFIX}evict-{key[1]}-{uuid.uuid4().hex}")
            try:
                final.rename(trash)
            except FileNotFoundError:
                pass  # already gone: just drop it from the index
            except OSError:
                # Still on disk and still indexed (usable); skip it this round.
                log.exception("repo cache could not evict %s/%s", *key)
                skipped.add(key)
                continue
            else:
                victims.append(trash)
                log.info("repo cache evicted %s/%s", *key)
            total -= self._entries.pop(key).size
        for trash in victims:
            try:
                await asyncio.to_thread(remove_snapshot, trash)
            except Exception:
                log.exception("repo cache could not delete %s", trash)

    async def drain(self) -> None:
        """Wait for background evictions (scheduled on release) to finish."""
        while self._background:
            # asyncio.wait, not gather: cancelling drain() must not cancel the evictions.
            await asyncio.wait(set(self._background))

    # --- internals ------------------------------------------------------------------

    def _path(self, key: Key) -> Path:
        return self._root / str(key[0]) / key[1]

    def _evict_soon(self) -> None:
        """Run `evict()` in the background (release is synchronous); errors are logged."""

        async def run() -> None:
            try:
                await self.evict(quiet=True)
            except Exception:
                log.exception("repo cache background eviction failed")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (shutdown): the next fetch or start() evicts instead
        if loop.is_closed():
            return
        task = loop.create_task(run())
        self._background.add(task)  # strong reference until it finishes
        task.add_done_callback(self._background.discard)

    def _now(self) -> tuple[float, int]:
        return (self._clock(), next(self._seq))

    def _touch(self, key: Key) -> None:
        entry = self._entries.get(key)
        if entry is not None:
            entry.last_used = self._now()

    def _fetch_done(self, key: Key, task: asyncio.Task[None]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled() and task.exception() is not None:
            # Retrieved here so an unawaited failure (all waiters cancelled) is not
            # reported as "exception never retrieved"; waiters still see it.
            log.info("repo cache fetch failed for %s/%s", key[0], key[1])

    async def _fetch(self, key: Key, fetch: Fetch) -> None:
        me = asyncio.current_task()
        try:
            await self._fetch_and_publish(key, fetch)
        except asyncio.CancelledError:
            if me is not None and me.cancelling():
                raise  # the fetch task itself is being cancelled (shutdown)
            # `fetch` raised CancelledError on its own: waiters must not read that as
            # their own cancellation.
            raise SnapshotError("snapshot fetch was cancelled") from None
        finally:
            # Synchronously, before any waiter resumes: a lease arriving next must not
            # find this finished task and reuse its result.
            if self._inflight.get(key) is me:
                del self._inflight[key]
        try:
            await self.evict()
        except Exception:
            log.exception("repo cache eviction failed after fetch")

    async def _fetch_and_publish(self, key: Key, fetch: Fetch) -> None:
        final = self._path(key)
        parent = final.parent
        tmp = parent / f"{TMP_PREFIX}{key[1]}-{uuid.uuid4().hex}"
        await asyncio.to_thread(parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(tmp.mkdir)
        try:
            await fetch(tmp)
            size = await asyncio.to_thread(_du, tmp)
            if os.path.isdir(final) and not os.path.islink(final):
                # Unindexed but present: only a completed rename publishes a final path,
                # so it is a whole snapshot. Adopt it and drop our copy.
                log.warning("repo cache adopting unindexed snapshot %s/%s", *key)
                await asyncio.to_thread(remove_snapshot, tmp)
                size = await asyncio.to_thread(_du, final)
            else:
                # Same parent, so this is an atomic rename even for a read-only tree.
                tmp.rename(final)
        except BaseException:
            await asyncio.to_thread(remove_snapshot, tmp)
            raise
        self._entries[key] = _Entry(size=size, last_used=self._now())

    def _scan(self) -> list[tuple[float, Key, int]]:
        """Blocking: find snapshots to adopt, delete temp leftovers."""
        found: list[tuple[float, Key, int]] = []
        self._root.mkdir(parents=True, exist_ok=True)
        for repo_dir in self._root.iterdir():
            if not (_REPO_DIR.match(repo_dir.name) and repo_dir.is_dir()):
                continue
            if repo_dir.is_symlink():
                continue
            for child in repo_dir.iterdir():
                if child.name.startswith(TMP_PREFIX):
                    if child.is_dir() and not child.is_symlink():
                        remove_snapshot(child)
                    else:
                        child.unlink()
                    log.info("repo cache removed stray temp %s", child)
                elif _SHA_DIR.match(child.name) and child.is_dir() and not child.is_symlink():
                    key = (int(repo_dir.name), child.name)
                    found.append((child.stat().st_mtime, key, _du(child)))
        return found

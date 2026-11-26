"""Runs admitted steps in the background (SPEC -> Agent step; T7).

`StepRunner.submit` starts exactly one asyncio task per step. The task leases the session's
snapshot for the whole `run_step` call (the shielded close included), builds the
`StepContext` from what was captured at admission (the step row and the owner's settings),
and runs the step with the app's sessionmaker, never a request's DB session.

A step always ends in exactly one waypoint or error node. If the snapshot cannot be leased
before `run_step` starts, the runner closes the step itself with an error node and
publishes it. Store errors raised because someone else already closed or removed the step
(startup recovery, a purge) are logged by type only.

Tasks are tracked per session: `cancel_session` cancels and awaits a session's tasks and
then ends its SSE streams (session deletion); `shutdown` does the same for every task
(lifespan shutdown, before the DB engine closes). `recover` marks the steps a previous
process left `queued` or `running` as interrupted (startup).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ahoy.github.http import GitHubError
from ahoy.github.repo_cache import Fetch, RepoCache
from ahoy.harness.events import EventBus, node_event, node_seq
from ahoy.harness.loop import ERR_CANCELLED, ERR_GITHUB, ERR_INTERNAL, StepContext, run_step
from ahoy.harness.schemas import GraphNode, QueuedEvent, Session, Steer, Step, User
from ahoy.providers.base import Provider
from ahoy.store import auth as auth_store
from ahoy.store import repo as store
from ahoy.store.repo import StoreError

log = logging.getLogger("ahoy.runner")

# The steer of the Overview step (SPEC -> US1), recorded like a captain's steer.
OVERVIEW_WEDGE: Final = "overview"
OVERVIEW_INSTRUCTION: Final = (
    "Give an overview of this repo: what it is, its main parts, and where to start reading."
)

ProviderFor = Callable[[Step], Provider]
SnapshotFetch = Callable[[Session], Fetch]

# Raised by run_step or by a close when the step was closed or removed by someone else.
_GONE: Final = (StoreError,)  # StepClosedError, StepStateError, NotFoundError are StoreErrors


class StepRunner:
    """Background step tasks: one per step, tracked per session."""

    def __init__(
        self,
        *,
        sessionmaker: Callable[[], async_sessionmaker[AsyncSession] | None],
        bus: EventBus,
        cache: RepoCache,
        snapshot_fetch: SnapshotFetch,
        provider_for: ProviderFor,
        provider_name: str,
    ) -> None:
        # A callable, so a sessionmaker installed later (tests) is picked up.
        self._sessionmaker = sessionmaker
        self.bus = bus
        self.cache = cache
        self.snapshot_fetch = snapshot_fetch
        self.provider_for = provider_for
        self.provider_name = provider_name  # recorded on each step row
        self._tasks: dict[UUID, asyncio.Task[None]] = {}  # step id -> task
        self._by_session: dict[UUID, set[UUID]] = {}  # session id -> step ids

    # --- introspection ----------------------------------------------------------------

    def tasks(self, session_id: UUID | None = None) -> list[asyncio.Task[None]]:
        if session_id is None:
            return list(self._tasks.values())
        return [self._tasks[s] for s in self._by_session.get(session_id, ()) if s in self._tasks]

    async def drain(self) -> None:
        """Wait until no step task is left (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # --- lifecycle --------------------------------------------------------------------

    async def admit(
        self,
        db: AsyncSession,
        session: Session,
        owner: User,
        *,
        parent_node_id: UUID | None,
        wedge: str,
        instruction: str,
        allow_command: str | None = None,
    ) -> Step:
        """Insert the `queued` step (owner's mode and model), announce it, start it."""
        step = await store.create_step(
            db,
            session_id=session.id,
            parent_node_id=parent_node_id,
            steered_by=owner.id,
            wedge=wedge,
            instruction=instruction,
            allow_command=allow_command,
            mode=owner.settings.mode,
            provider=self.provider_name,
            model=owner.settings.model,
        )
        self.bus.publish(step.id, QueuedEvent(seq=0))
        self.submit(step, session, owner)
        return step

    def submit(self, step: Step, session: Session, owner: User) -> None:
        """Start the step's task, unless one is already running for it. A step submitted
        again after its task ended is refused by the store (start_step takes only a queued
        step) and logged, never run twice."""
        if step.id in self._tasks:
            return
        task = asyncio.create_task(self._run(step, session, owner), name=f"step-{step.id}")
        self._tasks[step.id] = task
        self._by_session.setdefault(session.id, set()).add(step.id)
        task.add_done_callback(lambda _t: self._forget(step.id, session.id))

    def _forget(self, step_id: UUID, session_id: UUID) -> None:
        self._tasks.pop(step_id, None)
        ids = self._by_session.get(session_id)
        if ids is not None:
            ids.discard(step_id)
            if not ids:
                del self._by_session[session_id]

    async def cancel_session(self, session_id: UUID) -> None:
        """Cancel and await the session's step tasks, then end its event streams."""
        await _cancel_all(self.tasks(session_id))
        self.bus.close_session_streams(session_id)

    async def shutdown(self) -> None:
        """Cancel and await every step task (each closes its step with an error node)."""
        await _cancel_all(self.tasks())

    async def recover(self) -> int:
        """Startup: every step left queued or running gets an interrupted error node, then
        every ready session without an Overview step (the previous process died between
        `ready` and the Overview insert) gets its Overview. Returns the interrupted count."""
        maker = self._sessionmaker()
        if maker is None:
            return 0
        count = 0
        async with maker() as db:
            for step in await store.open_steps(db):
                try:
                    await store.mark_interrupted(db, step.id)
                    count += 1
                except StoreError as exc:  # closed or purged meanwhile
                    log.info("step %s not interrupted (%s)", step.id, type(exc).__name__)
            if count:
                log.info("marked %d interrupted step(s)", count)
            for session in await store.ready_sessions_without_overview(db):
                owner = await auth_store.get_user(db, session.owner_id)
                if owner is not None:
                    await self.start_overview(db, session, owner)
        return count

    async def start_overview(self, db: AsyncSession, session: Session, owner: User) -> None:
        """Admit and start the session's Overview step (it hangs off the root). A second
        one is refused by the one-Overview index and only logged."""
        try:
            await self.admit(
                db,
                session,
                owner,
                parent_node_id=None,
                wedge=OVERVIEW_WEDGE,
                instruction=OVERVIEW_INSTRUCTION,
            )
        except StoreError as exc:  # an Overview exists already, or the session is gone
            log.info("session %s: Overview not started (%s)", session.id, type(exc).__name__)

    # --- one step ---------------------------------------------------------------------

    async def _run(self, step: Step, session: Session, owner: User) -> None:
        maker = self._sessionmaker()
        if maker is None:
            log.error("step %s: no database", step.id)
            return
        started = False  # run_step owns the step's close from here on
        try:
            fetch = self.snapshot_fetch(session)
            async with self.cache.lease(session.repo_id, session.commit_sha, fetch) as root:
                ctx = self._context(step, session, owner, root)
                started = True
                await run_step(maker, ctx, self.bus)
        except asyncio.CancelledError:
            if not started:
                await self._close_shielded(maker, step, ERR_CANCELLED)
            raise
        except _GONE as exc:
            log.info("step %s ended elsewhere (%s)", step.id, type(exc).__name__)
        except Exception as exc:
            if started:
                log.error("step %s runner failed (%s)", step.id, type(exc).__name__)
                return
            log.warning("step %s: snapshot unavailable (%s)", step.id, type(exc).__name__)
            text = ERR_GITHUB if isinstance(exc, GitHubError) else ERR_INTERNAL
            await self._close_shielded(maker, step, text)

    def _context(self, step: Step, session: Session, owner: User, root: Path) -> StepContext:
        return StepContext(
            step=step,
            session=session,
            mode=step.mode,  # the owner's mode, captured on the step row at admission
            allowlist=tuple(owner.settings.allowlist),
            model=step.model,
            provider=self.provider_for(step),
            snapshot_root=root,
        )

    async def _close_shielded(
        self, maker: async_sessionmaker[AsyncSession], step: Step, text: str
    ) -> None:
        """Close a step that never reached run_step with one error node, and publish it."""
        task = asyncio.ensure_future(self._close(maker, step, text))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.wait({task})  # a second cancel: still let the close land
            raise

    async def _close(self, maker: async_sessionmaker[AsyncSession], step: Step, text: str) -> None:
        draft = GraphNode(
            id=uuid4(),
            session_id=step.session_id,
            step_id=step.id,
            parent_id=None,  # close_step_at_tip hangs it off the step's tip
            kind="error",
            error=text,
            steer=Steer(
                wedge=step.wedge,
                instruction=step.instruction,
                allow_command=step.allow_command,
                steered_by=step.steered_by,
            ),
            mode=step.mode,
            model=step.model,
            created_at=datetime.now(UTC),  # replaced by the DB clock on insert
        )
        try:
            async with maker() as db:
                _closed, stored, k = await store.close_step_at_tip(db, step.id, "error", draft)
        except _GONE as exc:
            log.info("step %s already closed (%s)", step.id, type(exc).__name__)
            return
        except Exception as exc:
            log.error("closing step %s failed (%s)", step.id, type(exc).__name__)
            return
        self.bus.publish(step.id, node_event(stored, node_seq(k)))


async def _cancel_all(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

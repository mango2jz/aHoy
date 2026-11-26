"""Store writers against purge on two real connections (committed data in ahoy_test).

Writers lock the session row FOR KEY SHARE before the step row; purge_sessions() locks the
session rows FOR UPDATE first. Whichever side gets there first, the other waits: no deadlock.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from ahoy.harness.schemas import GraphNode, Session, Step
from ahoy.store import repo
from ahoy.store.db import make_engine, make_sessionmaker

pytestmark = pytest.mark.db

Writer = Callable[[AsyncSession, Session, Step, GraphNode], Awaitable[Any]]


@pytest.fixture
async def sm(test_database: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(test_database, poolclass=NullPool)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def graph(
    sm: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[UUID, Session, GraphNode, Step]]:
    """A committed user, session, root and running step; everything is removed afterwards."""
    async with sm() as db:
        uid = uuid4()
        await db.execute(
            text(
                "INSERT INTO users (id, github_id, github_login, avatar_url, settings)"
                " VALUES (:id, :g, :l, '', '{}'::jsonb)"
            ),
            {"id": uid, "g": uid.int % 10**12, "l": f"race-{uid.hex[:8]}"},
        )
        await db.commit()
        session, root = await repo.create_session(
            db,
            owner_id=uid,
            installation_id=1,
            repo_id=1,
            repo_full_name="a/b",
            ref="main",
            commit_sha="a" * 40,
            private=False,
            model="m",
        )
        step = await repo.create_step(
            db,
            session_id=session.id,
            parent_node_id=root.id,
            steered_by=uid,
            wedge="explain",
            instruction="x",
            mode="read_only",
            provider="mock",
            model="m",
            status="running",
        )
    yield uid, session, root, step
    async with sm() as db:
        await repo.purge(db, uid, [session.id])
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
        await db.commit()


def error_node(session: Session, step: Step, parent: UUID) -> GraphNode:
    return GraphNode(
        id=uuid4(),
        session_id=session.id,
        step_id=step.id,
        parent_id=parent,
        kind="error",
        error="boom",
        model="m",
        created_at=datetime.now(UTC),
    )


async def append(db: AsyncSession, s: Session, st: Step, root: GraphNode) -> Any:
    return await repo.append_node(db, error_node(s, st, root.id))


async def close(db: AsyncSession, s: Session, st: Step, root: GraphNode) -> Any:
    return await repo.close_step(db, st.id, "done", error_node(s, st, root.id))


async def interrupt(db: AsyncSession, s: Session, st: Step, root: GraphNode) -> Any:
    return await repo.mark_interrupted(db, st.id)


WRITERS = [pytest.param(w, id=w.__name__) for w in (append, close, interrupt)]


async def settle(*tasks: Awaitable[Any]) -> list[Any]:
    # A deadlock would surface as DBAPIError 40P01 within deadlock_timeout (1 s).
    return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=20)


@pytest.mark.parametrize("writer", WRITERS)
async def test_writer_first_then_purge(
    writer: Writer,
    sm: async_sessionmaker[AsyncSession],
    graph: tuple[UUID, Session, GraphNode, Step],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, session, root, step = graph
    insert = repo._insert_node

    async def slow_insert(db: AsyncSession, node: GraphNode) -> GraphNode:
        await asyncio.sleep(0.5)  # holding the session and step locks
        return await insert(db, node)

    monkeypatch.setattr(repo, "_insert_node", slow_insert)
    async with sm() as a, sm() as b:
        written = asyncio.create_task(writer(a, session, step, root))
        await asyncio.sleep(0.2)
        purged = asyncio.create_task(repo.purge(b, uid, [session.id]))
        results = await settle(written, purged)

    assert not any(isinstance(r, BaseException) for r in results), results
    assert results[1] == 1
    async with sm() as c:
        assert await repo.load_nodes(c, session.id) == []


@pytest.mark.parametrize("writer", WRITERS)
async def test_purge_first_then_writer(
    writer: Writer,
    sm: async_sessionmaker[AsyncSession],
    graph: tuple[UUID, Session, GraphNode, Step],
) -> None:
    uid, session, root, step = graph
    async with sm() as a, sm() as b:
        # purge_sessions()' first lock, held while the writer starts.
        await b.execute(
            text("SELECT 1 FROM sessions WHERE id = :id FOR UPDATE"), {"id": session.id}
        )
        written = asyncio.create_task(writer(a, session, step, root))
        await asyncio.sleep(0.3)
        assert not written.done()  # waiting on the session row, not deadlocked

        async def finish_purge() -> int:
            n = (
                await b.execute(
                    text("SELECT purge_sessions(:o, ARRAY[:s]::uuid[])"),
                    {"o": uid, "s": session.id},
                )
            ).scalar_one()
            await b.commit()
            return int(n)

        results = await settle(written, finish_purge())

    assert isinstance(results[0], repo.NotFoundError), results
    assert results[1] == 1


@pytest.mark.parametrize("first", ["append", "close"])
async def test_close_at_tip_races_append_on_the_same_step(
    first: str,
    sm: async_sessionmaker[AsyncSession],
    graph: tuple[UUID, Session, GraphNode, Step],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whichever writer takes the step lock first wins; the other waits, no deadlock, and
    the final node always hangs off the real tip."""
    _uid, session, root, step = graph
    insert = repo._insert_node

    async def slow_insert(db: AsyncSession, node: GraphNode) -> GraphNode:
        await asyncio.sleep(0.5)  # holding the session and step locks
        return await insert(db, node)

    monkeypatch.setattr(repo, "_insert_node", slow_insert)
    tool = GraphNode(
        id=uuid4(),
        session_id=session.id,
        step_id=step.id,
        parent_id=root.id,
        kind="tool",
        tool={"name": "list_dir", "args": {}, "output": "x"},  # type: ignore[arg-type]
        model="m",
        created_at=datetime.now(UTC),
    )
    final = error_node(session, step, root.id)
    async with sm() as a, sm() as b:

        async def do_append() -> Any:
            return await repo.append_node(a, tool)

        async def do_close() -> Any:
            return await repo.close_step_at_tip(b, step.id, "error", final)

        one, two = (do_append, do_close) if first == "append" else (do_close, do_append)
        started = asyncio.create_task(one())
        await asyncio.sleep(0.2)
        results = await settle(started, two())
    appended, closed = results if first == "append" else results[::-1]

    async with sm() as c:
        nodes = await repo.step_nodes(c, step.id)
        now = await repo.get_step(c, step.id)
    assert now is not None and now.status == "error"
    if first == "append":
        assert isinstance(appended, GraphNode), results
        _step, stored, k = closed
        assert [n.kind for n in nodes] == ["tool", "error"]
        assert (stored.parent_id, k) == (appended.id, 2)
    else:
        assert isinstance(appended, repo.StepStateError), results
        _step, stored, k = closed
        assert [n.kind for n in nodes] == ["error"]
        assert (stored.parent_id, k) == (root.id, 1)

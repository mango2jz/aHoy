"""Store API against the real test database, connected as ahoy_app (T36)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from ahoy.harness.schemas import (
    GraphNode,
    Session,
    Steer,
    Step,
    ToolCall,
    Waypoint,
)
from ahoy.store import repo
from ahoy.store.db import make_engine, make_sessionmaker
from ahoy.store.models import Base, GraphNodeRow, SessionRow, StepRow

pytestmark = pytest.mark.db

MakeUser = Callable[[], Awaitable[UUID]]

WAYPOINT = Waypoint(
    title="t",
    summary="s",
    key_points=["a.", "b."],
    code_refs=[],
    next_moves=[],
    confidence="low",
)

# What callers put in created_at; the store must replace it with the DB clock.
CALLER_CLOCK = datetime(2001, 1, 1, tzinfo=UTC)


def node(
    session_id: UUID,
    step_id: UUID | None,
    parent_id: UUID | None,
    kind: str,
    **kw: Any,
) -> GraphNode:
    payload: dict[str, Any] = {}
    if kind == "tool":
        payload["tool"] = ToolCall(name="list_dir", args={"path": "."}, output="a\nb")
    elif kind == "waypoint":
        payload["waypoint"] = WAYPOINT
        payload["mode"] = "safe"
    elif kind == "error":
        payload["error"] = "boom"
    return GraphNode.model_validate(
        {
            "id": uuid4(),
            "session_id": session_id,
            "step_id": step_id,
            "parent_id": parent_id,
            "kind": kind,
            "model": "mock/m",
            "created_at": CALLER_CLOCK,
            **payload,
            **kw,
        }
    )


async def new_session(db: AsyncSession, owner: UUID) -> tuple[Session, GraphNode]:
    return await repo.create_session(
        db,
        owner_id=owner,
        installation_id=7,
        repo_id=42,
        repo_full_name="fixture/demo",
        ref="main",
        commit_sha="a" * 40,
        private=False,
        model="mock/m",
    )


async def new_step(
    db: AsyncSession, session: Session, parent: UUID | None, *, start: bool = True
) -> Step:
    step = await repo.create_step(
        db,
        session_id=session.id,
        parent_node_id=parent,
        steered_by=session.owner_id,
        wedge="explain",
        instruction="go",
        mode="safe",
        provider="mock",
        model="mock/m",
    )
    return await repo.start_step(db, step.id) if start else step


async def with_root(db: AsyncSession, make_user: MakeUser) -> tuple[Session, GraphNode]:
    return await new_session(db, await make_user())


async def db_now(db: AsyncSession) -> datetime:
    value: datetime = (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()
    return value


# --- field-set parity ----------------------------------------------------------------


def _columns(row: type[Any]) -> set[str]:
    return set(row.__table__.columns.keys())


def test_graph_node_fields_match_columns() -> None:
    assert _columns(GraphNodeRow) == set(GraphNode.model_fields)


def test_step_fields_match_columns() -> None:
    assert _columns(StepRow) == set(Step.model_fields)


def test_session_fields_match_columns() -> None:
    assert _columns(SessionRow) == set(Session.model_fields)


# --- sessions ------------------------------------------------------------------------


async def test_create_session_and_set_status(db: AsyncSession, make_user: MakeUser) -> None:
    owner = await make_user()
    session, _ = await new_session(db, owner)
    assert isinstance(session, Session)
    assert (session.owner_id, session.status, session.shared) == (owner, "preparing", False)
    assert session.created_at.tzinfo is not None

    failed = await repo.set_session_status(db, session.id, "failed", "too large")
    assert (failed.id, failed.status, failed.status_reason) == (session.id, "failed", "too large")
    ready = await repo.set_session_status(db, session.id, "ready")
    assert (ready.status, ready.status_reason) == ("ready", None)


async def test_create_session_inserts_the_root(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await new_session(db, await make_user())
    assert isinstance(root, GraphNode)
    assert (root.kind, root.step_id, root.parent_id, root.session_id, root.model) == (
        "root",
        None,
        None,
        session.id,
        "mock/m",
    )
    assert await repo.load_nodes(db, session.id) == [root]


async def test_create_session_is_atomic_with_its_root(
    db: AsyncSession, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await make_user()

    async def fail(*_: object) -> GraphNode:
        raise RuntimeError("crash before the root insert")

    monkeypatch.setattr(repo, "_insert_node", fail)
    with pytest.raises(RuntimeError):
        await new_session(db, owner)
    count = (
        await db.execute(text("SELECT count(*) FROM sessions WHERE owner_id = :o"), {"o": owner})
    ).scalar_one()
    assert count == 0


async def test_set_session_status_unknown_session(db: AsyncSession) -> None:
    with pytest.raises(repo.NotFoundError):
        await repo.set_session_status(db, uuid4(), "ready")


# --- steps ---------------------------------------------------------------------------


async def test_create_step_and_open_steps(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    first = await new_step(db, session, None, start=False)
    second = await new_step(db, session, root.id)
    assert isinstance(first, Step)
    assert (first.status, first.parent_node_id, first.started_at) == ("queued", None, None)
    assert second.parent_node_id == root.id

    assert [s.id for s in await repo.open_steps(db)] == [first.id, second.id]
    await repo.close_step(db, first.id, "done", node(session.id, first.id, root.id, "waypoint"))
    assert [s.id for s in await repo.open_steps(db)] == [second.id]


async def test_create_step_must_be_open(db: AsyncSession, make_user: MakeUser) -> None:
    session, _ = await with_root(db, make_user)
    with pytest.raises(repo.StoreError):
        await repo.create_step(
            db,
            session_id=session.id,
            parent_node_id=None,
            steered_by=session.owner_id,
            wedge="w",
            instruction="i",
            mode="safe",
            provider="mock",
            model="m",
            status="done",
        )


async def test_start_step(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    queued = await new_step(db, session, root.id, start=False)
    before = await db_now(db)
    running = await repo.start_step(db, queued.id)
    assert running.status == "running"
    assert running.started_at is not None and running.started_at >= before

    with pytest.raises(repo.StepStateError, match="running"):
        await repo.start_step(db, queued.id)
    await repo.close_step(db, queued.id, "done", node(session.id, queued.id, root.id, "waypoint"))
    with pytest.raises(repo.StepStateError, match="done"):
        await repo.start_step(db, queued.id)
    with pytest.raises(repo.NotFoundError):
        await repo.start_step(db, uuid4())


async def test_close_step(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    final = node(session.id, step.id, root.id, "waypoint")

    closed, stored = await repo.close_step(db, step.id, "done", final)
    assert (closed.status, closed.closed_at is not None) == ("done", True)
    assert stored.id == final.id
    assert stored.created_at != CALLER_CLOCK
    assert await repo.step_nodes(db, step.id) == [stored]


async def test_close_step_accepts_a_queued_step(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id, start=False)
    closed, _ = await repo.close_step(
        db, step.id, "error", node(session.id, step.id, root.id, "error")
    )
    assert closed.status == "error"


async def test_close_step_refuses_closed_step(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    await repo.close_step(db, step.id, "done", node(session.id, step.id, root.id, "waypoint"))

    again = node(session.id, step.id, root.id, "error")
    with pytest.raises(repo.StepClosedError):
        await repo.close_step(db, step.id, "error", again)
    assert again.id not in {n.id for n in await repo.load_nodes(db, session.id)}


async def test_close_step_is_atomic(
    db: AsyncSession, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    final = node(session.id, step.id, root.id, "waypoint")

    async def fail(*_: object) -> Step:
        raise RuntimeError("crash after the node insert")

    # The node is inserted, then closing the step fails.
    monkeypatch.setattr(repo, "_finish_step", fail)
    with pytest.raises(RuntimeError):
        await repo.close_step(db, step.id, "done", final)

    assert await repo.step_nodes(db, step.id) == []
    assert [s.id for s in await repo.open_steps(db)] == [step.id]


async def test_close_step_rejects_inconsistent_node(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    other = await new_step(db, session, root.id)
    with pytest.raises(repo.StoreError):  # another step's node
        await repo.close_step(db, step.id, "done", node(session.id, other.id, root.id, "waypoint"))
    with pytest.raises(repo.StoreError):  # a tool node cannot end a step
        await repo.close_step(db, step.id, "done", node(session.id, step.id, root.id, "tool"))
    with pytest.raises(repo.StoreError):  # not a closed status
        await repo.close_step(
            db, step.id, "running", node(session.id, step.id, root.id, "waypoint")
        )
    assert await repo.step_nodes(db, step.id) == []


async def test_mark_interrupted(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    tool = await repo.append_node(db, node(session.id, step.id, root.id, "tool"))

    closed, err = await repo.mark_interrupted(db, step.id)
    assert (closed.id, closed.status, closed.closed_at is not None) == (
        step.id,
        "interrupted",
        True,
    )
    assert (err.kind, err.error, err.parent_id, err.mode) == (
        "error",
        "interrupted",
        tool.id,
        "safe",
    )
    assert [n.id for n in await repo.step_nodes(db, step.id)] == [tool.id, err.id]
    assert await repo.open_steps(db) == []
    with pytest.raises(repo.StepClosedError):
        await repo.mark_interrupted(db, step.id)


async def test_mark_interrupted_queued_step_hangs_off_parent(
    db: AsyncSession, make_user: MakeUser
) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id, start=False)
    closed, err = await repo.mark_interrupted(db, step.id)
    assert closed.status == "interrupted"
    assert err.parent_id == root.id


# --- nodes ---------------------------------------------------------------------------


async def test_append_and_load_nodes(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    steer = Steer(wedge="explain", instruction="go", steered_by=session.owner_id)
    tool = await repo.append_node(db, node(session.id, step.id, root.id, "tool", steer=steer))
    assert isinstance(tool, GraphNode)
    assert tool.steer == steer

    other_session, other_root = await with_root(db, make_user)
    assert [n.id for n in await repo.load_nodes(db, session.id)] == [root.id, tool.id]
    assert [n.id for n in await repo.load_nodes(db, other_session.id)] == [other_root.id]


async def test_node_created_at_comes_from_the_db_clock(
    db: AsyncSession, make_user: MakeUser
) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    before = await db_now(db)
    appended = [
        await repo.append_node(db, node(session.id, step.id, root.id, "tool")) for _ in range(3)
    ]
    assert all(n.created_at >= before for n in appended)
    stamps = [n.created_at for n in appended]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3
    assert [n.id for n in await repo.load_nodes(db, session.id)] == [
        root.id,
        *(n.id for n in appended),
    ]


async def test_append_node_refuses_root(db: AsyncSession, make_user: MakeUser) -> None:
    session, _ = await with_root(db, make_user)
    with pytest.raises(repo.StoreError, match="root"):
        await repo.append_node(db, node(session.id, None, None, "root"))
    bad = node(session.id, uuid4(), None, "tool").model_copy(update={"step_id": None})
    with pytest.raises(repo.StoreError):
        await repo.append_node(db, bad)
    # The unique index stays as the backstop against a second root.
    with pytest.raises(IntegrityError, match="uq_graph_nodes_one_root"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO graph_nodes (session_id, kind, model) VALUES (:sid, 'root', 'm')"
                ),
                {"sid": session.id},
            )


async def test_append_node_needs_a_running_step(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    queued = await new_step(db, session, root.id, start=False)
    with pytest.raises(repo.StepStateError, match="queued"):
        await repo.append_node(db, node(session.id, queued.id, root.id, "tool"))

    done = await new_step(db, session, root.id)
    await repo.close_step(db, done.id, "done", node(session.id, done.id, root.id, "waypoint"))
    with pytest.raises(repo.StepStateError, match="done"):
        await repo.append_node(db, node(session.id, done.id, root.id, "tool"))

    with pytest.raises(repo.NotFoundError):
        await repo.append_node(db, node(session.id, uuid4(), root.id, "tool"))

    other, _ = await with_root(db, make_user)
    running = await new_step(db, session, root.id)
    with pytest.raises(repo.StoreError, match="another session"):
        await repo.append_node(db, node(other.id, running.id, None, "tool"))
    assert [n.id for n in await repo.load_nodes(db, session.id)] == [
        root.id,
        *(n.id for n in await repo.step_nodes(db, done.id)),
    ]


async def test_a_node_cannot_be_its_own_parent(db: AsyncSession, make_user: MakeUser) -> None:
    # The parent FK accepts a self-reference, and root_path would loop on it.
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    for kind, call in (
        ("tool", lambda n: repo.append_node(db, n)),
        ("waypoint", lambda n: repo.close_step(db, step.id, "done", n)),
    ):
        loop = node(session.id, step.id, None, kind)
        loop = loop.model_copy(update={"parent_id": loop.id})
        with pytest.raises(repo.StoreError, match="own parent"):
            await call(loop)
    assert await repo.step_nodes(db, step.id) == []


async def test_step_nodes_and_children(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    a = await new_step(db, session, root.id)
    b = await new_step(db, session, root.id)
    a1 = await repo.append_node(db, node(session.id, a.id, root.id, "tool"))
    b1 = await repo.append_node(db, node(session.id, b.id, root.id, "tool"))
    a2 = await repo.append_node(db, node(session.id, a.id, a1.id, "tool"))

    assert [n.id for n in await repo.step_nodes(db, a.id)] == [a1.id, a2.id]
    assert [n.id for n in await repo.step_nodes(db, b.id)] == [b1.id]
    assert [n.id for n in await repo.children(db, root.id)] == [a1.id, b1.id]
    assert [n.id for n in await repo.children(db, a1.id)] == [a2.id]
    assert await repo.children(db, a2.id) == []


async def test_root_path_excludes_sibling_branches(db: AsyncSession, make_user: MakeUser) -> None:
    # root -> w1 -> {left: l1 -> l2, right: r1 -> r2}
    session, root = await with_root(db, make_user)
    s1 = await new_step(db, session, root.id)
    w1 = node(session.id, s1.id, root.id, "waypoint")
    await repo.close_step(db, s1.id, "done", w1)

    left = await new_step(db, session, w1.id)
    right = await new_step(db, session, w1.id)
    l1 = await repo.append_node(db, node(session.id, left.id, w1.id, "tool"))
    r1 = await repo.append_node(db, node(session.id, right.id, w1.id, "tool"))
    l2 = node(session.id, left.id, l1.id, "waypoint")
    r2 = node(session.id, right.id, r1.id, "waypoint")
    await repo.close_step(db, left.id, "done", l2)
    await repo.close_step(db, right.id, "done", r2)

    assert [n.id for n in await repo.root_path(db, l2.id)] == [root.id, w1.id, l1.id, l2.id]
    assert [n.id for n in await repo.root_path(db, r2.id)] == [root.id, w1.id, r1.id, r2.id]
    assert [n.id for n in await repo.root_path(db, w1.id)] == [root.id, w1.id]
    assert [n.id for n in await repo.root_path(db, root.id)] == [root.id]
    assert await repo.root_path(db, uuid4()) == []


# --- purge ---------------------------------------------------------------------------


async def test_purge(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    await repo.close_step(db, step.id, "done", node(session.id, step.id, root.id, "waypoint"))
    keep, keep_root = await with_root(db, make_user)

    assert await repo.purge(db, keep.owner_id, [session.id]) == 0  # not that owner's
    assert await repo.purge(db, session.owner_id, [session.id, keep.id]) == 1
    assert await repo.load_nodes(db, session.id) == []
    assert await repo.open_steps(db) == []
    assert [n.id for n in await repo.load_nodes(db, keep.id)] == [keep_root.id]
    assert await repo.purge(db, session.owner_id, []) == 0
    # A steer racing a purge finds the session gone (not a raw FK error).
    with pytest.raises(repo.NotFoundError, match="session"):
        await new_step(db, session, None, start=False)


# --- branch integrity ----------------------------------------------------------------


async def test_nodes_attach_only_to_their_own_branch(db: AsyncSession, make_user: MakeUser) -> None:
    # root -> w1 (step s1); two sibling steps off w1.
    session, root = await with_root(db, make_user)
    s1 = await new_step(db, session, root.id)
    _, w1 = await repo.close_step(db, s1.id, "done", node(session.id, s1.id, root.id, "waypoint"))
    left = await new_step(db, session, w1.id)
    right = await new_step(db, session, w1.id)
    l1 = await repo.append_node(db, node(session.id, left.id, w1.id, "tool"))
    await repo.append_node(db, node(session.id, left.id, l1.id, "tool"))  # same step: fine

    refused = "parent must be"
    with pytest.raises(repo.StoreError, match=refused):  # a sibling branch's node
        await repo.append_node(db, node(session.id, right.id, l1.id, "tool"))
    with pytest.raises(repo.StoreError, match=refused):  # the root, but the step is off w1
        await repo.append_node(db, node(session.id, right.id, root.id, "tool"))
    with pytest.raises(repo.StoreError, match="needs a parent"):
        await repo.append_node(db, node(session.id, right.id, None, "tool"))
    with pytest.raises(repo.StoreError, match=refused):  # final node on a sibling branch
        await repo.close_step(db, right.id, "done", node(session.id, right.id, l1.id, "waypoint"))
    with pytest.raises(repo.StoreError, match=refused):  # an unknown node
        await repo.append_node(db, node(session.id, right.id, uuid4(), "tool"))
    assert await repo.step_nodes(db, right.id) == []


async def test_overview_step_hangs_off_the_root(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    overview = await new_step(db, session, None)
    first = await repo.append_node(db, node(session.id, overview.id, root.id, "tool"))
    assert first.parent_id == root.id

    _, err = await repo.mark_interrupted(db, overview.id)
    assert err.parent_id == first.id

    # An Overview step with no nodes yet: the error node hangs off the root.
    other, other_root = await with_root(db, make_user)
    queued = await new_step(db, other, None, start=False)
    _, err = await repo.mark_interrupted(db, queued.id)
    assert err.parent_id == other_root.id


async def test_one_overview_step_per_session(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    await new_step(db, session, None)
    with pytest.raises(repo.StoreError, match="already has an Overview step"):
        await new_step(db, session, None)
    await new_step(db, session, root.id)  # steps with a parent are not limited
    other, _ = await with_root(db, make_user)
    await new_step(db, other, None)  # one per session, not one in total
    assert len(await repo.open_steps(db)) == 3


async def test_create_step_parent_in_another_session(db: AsyncSession, make_user: MakeUser) -> None:
    session, _ = await with_root(db, make_user)
    _, other_root = await with_root(db, make_user)
    with pytest.raises(repo.StoreError, match="does not exist in that session"):
        await new_step(db, session, other_root.id)
    with pytest.raises(repo.StoreError, match="does not exist in that session"):
        await new_step(db, session, uuid4())
    assert await repo.open_steps(db) == []


# --- cycles --------------------------------------------------------------------------


async def _insert_raw(db: AsyncSession, sql: str, **params: Any) -> None:
    async with db.begin_nested():
        await db.execute(text(sql), params)


async def test_self_parent_is_refused_by_the_database(
    db: AsyncSession, make_user: MakeUser
) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    nid = uuid4()
    with pytest.raises(IntegrityError, match="ck_graph_nodes_parent_not_self"):
        await _insert_raw(
            db,
            "INSERT INTO graph_nodes (id, session_id, step_id, parent_id, kind, error, model)"
            " VALUES (:id, :sid, :st, :id, 'error', 'x', 'm')",
            id=nid,
            sid=session.id,
            st=step.id,
        )


async def test_root_path_detects_a_cycle(db: AsyncSession, make_user: MakeUser) -> None:
    # One multi-row INSERT as ahoy_app: the FKs are checked at the end of the statement, so
    # a -> b -> a is accepted by the database. root_path must stop and refuse it.
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    a, b = uuid4(), uuid4()
    await _insert_raw(
        db,
        "INSERT INTO graph_nodes (id, session_id, step_id, parent_id, kind, error, model)"
        " VALUES (:a, :sid, :st, :b, 'error', 'x', 'm'), (:b, :sid, :st, :a, 'error', 'x', 'm')",
        a=a,
        b=b,
        sid=session.id,
        st=step.id,
    )
    with pytest.raises(repo.StoreError, match="cycle"):
        await repo.root_path(db, a)


# --- read transactions ---------------------------------------------------------------


async def test_reads_end_their_transaction(db: AsyncSession, make_user: MakeUser) -> None:
    session, root = await with_root(db, make_user)
    step = await new_step(db, session, root.id)
    for read in (
        repo.open_steps(db),
        repo.load_nodes(db, session.id),
        repo.step_nodes(db, step.id),
        repo.children(db, root.id),
        repo.root_path(db, root.id),
    ):
        await read
        assert not db.in_transaction()


async def test_reads_leave_the_connection_idle(test_database: URL) -> None:
    # A real pooled connection outside the test transaction, watched from a second engine.
    # One pooled connection, so the session gets the same backend for every transaction.
    engine = make_engine(test_database, pool_size=1, max_overflow=0)
    watch = make_engine(test_database, poolclass=NullPool)
    try:
        async with make_sessionmaker(engine)() as reader:
            pid = (await reader.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await reader.commit()
            await repo.load_nodes(reader, uuid4())
            assert (await reader.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid
            await reader.commit()
            await repo.root_path(reader, uuid4())
            async with watch.connect() as watcher:
                state = (
                    await watcher.execute(
                        text("SELECT state FROM pg_stat_activity WHERE pid = :p"), {"p": pid}
                    )
                ).scalar_one()
            assert state == "idle"
    finally:
        await engine.dispose()
        await watch.dispose()


# --- purge retry ---------------------------------------------------------------------


class _Deadlock(Exception):
    sqlstate = repo.DEADLOCK_DETECTED


async def test_purge_retries_once_on_deadlock(
    db: AsyncSession, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _ = await with_root(db, make_user)
    write = repo._write
    calls = {"n": 0}

    async def flaky(db_: AsyncSession, work: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise DBAPIError("SELECT purge_sessions(...)", None, _Deadlock())
        return await write(db_, work)

    monkeypatch.setattr(repo, "_write", flaky)
    assert await repo.purge(db, session.owner_id, [session.id]) == 1
    assert calls["n"] == 2


async def test_purge_does_not_retry_other_errors(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    async def failing(db_: AsyncSession, work: Any) -> Any:
        calls["n"] += 1
        raise DBAPIError("SELECT purge_sessions(...)", None, Exception("other"))

    monkeypatch.setattr(repo, "_write", failing)
    with pytest.raises(DBAPIError):
        await repo.purge(db, uuid4(), [uuid4()])
    assert calls["n"] == 1


async def test_check_constraint_names_match_the_migration(db: AsyncSession) -> None:
    rows = await db.execute(
        text(
            "SELECT conrelid::regclass::text, conname FROM pg_constraint"
            " WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
        )
    )
    in_db = {(table, name) for table, name in rows.all()}

    def full(table: str, name: str) -> str:  # the "ck" naming convention, once applied
        prefix = f"ck_{table}_"
        return name if name.startswith(prefix) else prefix + name

    in_models = {
        (table.name, full(table.name, str(c.name)))
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint)
    }
    assert in_models == in_db
    assert ("graph_nodes", "ck_graph_nodes_parent_not_self") in in_db

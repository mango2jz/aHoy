"""Per-step event channels (T6): seq scheme, replay then live, resume, cleanup."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.harness.events import EventBus, node_event, node_seq, tool_started_seq
from ahoy.harness.loop import StepContext, run_step
from ahoy.harness.schemas import (
    AnswerTurn,
    GraphNode,
    QueuedEvent,
    StatusEvent,
    StepStatus,
    ToolCall,
    ToolStartedEvent,
    ToolTurn,
    TurnSchema,
    Waypoint,
)
from ahoy.providers.base import Message
from ahoy.store import repo
from ahoy.store.models import StepRow
from conftest import SameSession

MakeUser = Callable[[], Awaitable[UUID]]
NOW = datetime(2026, 1, 1, tzinfo=UTC)


# --- helpers -------------------------------------------------------------------------------


def tool_node(step_id: UUID, session_id: UUID) -> GraphNode:
    return GraphNode(
        id=uuid4(),
        session_id=session_id,
        step_id=step_id,
        parent_id=uuid4(),
        kind="tool",
        tool=ToolCall(name="list_dir", args={}, output="x"),
        model="m",
        created_at=NOW,
    )


def error_node(step_id: UUID, session_id: UUID) -> GraphNode:
    return GraphNode(
        id=uuid4(),
        session_id=session_id,
        step_id=step_id,
        parent_id=uuid4(),
        kind="error",
        error="boom",
        model="m",
        created_at=NOW,
    )


def started(k: int) -> ToolStartedEvent:
    return ToolStartedEvent(seq=tool_started_seq(k), name="list_dir", args={}, why="w")


def fixed(status: StepStatus, nodes: list[GraphNode]) -> dict[str, Any]:
    async def load_status() -> StepStatus:
        return status

    async def load_nodes() -> list[GraphNode]:
        return nodes

    return {"load_status": load_status, "load_nodes": load_nodes}


TIMEOUT = 5  # every await on a stream is bounded: a stream that never ends fails, not hangs


async def _drain(it: AsyncIterator[Any]) -> list[Any]:
    return [e async for e in it]


async def collect(it: AsyncIterator[Any]) -> list[Any]:
    return await asyncio.wait_for(_drain(it), TIMEOUT)


async def nxt(it: AsyncIterator[Any]) -> Any:
    return await asyncio.wait_for(anext(it), TIMEOUT)


async def within(aw: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(aw, TIMEOUT)


def seqs(events: list[Any]) -> list[tuple[str, int]]:
    return [(e.type, e.seq) for e in events]


# --- pure bus ------------------------------------------------------------------------------


def test_seq_scheme() -> None:
    assert [node_seq(k) for k in (1, 2, 3)] == [2, 4, 6]
    assert [tool_started_seq(k) for k in (1, 2, 3)] == [1, 3, 5]
    with pytest.raises(ValueError, match="1-based"):
        node_seq(0)


async def test_closed_step_replays_and_ends_without_transients() -> None:
    bus = EventBus()
    step, sess = uuid4(), uuid4()
    nodes = [tool_node(step, sess), tool_node(step, sess), error_node(step, sess)]
    events = await collect(bus.replay_then_live(step, 0, session_id=sess, **fixed("error", nodes)))
    assert seqs(events) == [("status", 0), ("tool_done", 2), ("tool_done", 4), ("error", 6)]
    assert bus.channel_count() == 0


async def test_last_event_id_resume_on_closed_step() -> None:
    bus = EventBus()
    step, sess = uuid4(), uuid4()
    nodes = [tool_node(step, sess), tool_node(step, sess), error_node(step, sess)]
    events = await collect(bus.replay_then_live(step, 4, session_id=sess, **fixed("error", nodes)))
    assert seqs(events) == [("status", 0), ("error", 6)]
    events = await collect(bus.replay_then_live(step, 6, session_id=sess, **fixed("error", nodes)))
    assert seqs(events) == [("status", 0)]  # nothing left; the step is closed


async def test_node_published_during_db_read_is_sent_once() -> None:
    """Committed before the read, published after the subscribe: replayed, live copy dropped."""
    bus = EventBus()
    step, sess = uuid4(), uuid4()
    first = tool_node(step, sess)

    async def load_status() -> StepStatus:
        return "running"

    async def load_nodes() -> list[GraphNode]:
        bus.publish(step, started(1))
        bus.publish(step, node_event(first, node_seq(1)))
        return [first]

    it = bus.replay_then_live(
        step, 0, session_id=sess, load_status=load_status, load_nodes=load_nodes
    )
    got = [await nxt(it), await nxt(it)]
    bus.publish(step, started(2))
    final = error_node(step, sess)
    bus.publish(step, node_event(final, node_seq(2)))
    got += await collect(it)
    assert seqs(got) == [("status", 0), ("tool_done", 2), ("tool_started", 3), ("error", 4)]
    assert bus.channel_count() == 0


async def test_seq_zero_events_pass_live() -> None:
    bus = EventBus()
    step, sess = uuid4(), uuid4()
    it = bus.replay_then_live(step, 0, session_id=sess, **fixed("queued", []))
    assert seqs([await nxt(it)]) == [("status", 0)]
    pending = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0)
    bus.publish(step, StatusEvent(seq=0, status="running"))
    assert seqs([await within(pending)]) == [("status", 0)]
    bus.publish(step, QueuedEvent(seq=0))
    assert (await nxt(it)).type == "queued"
    await it.aclose()
    assert bus.channel_count() == 0


async def test_close_session_streams() -> None:
    bus = EventBus()
    sess_a, sess_b = uuid4(), uuid4()
    steps_a, step_b = [uuid4(), uuid4()], uuid4()
    streams = [
        bus.replay_then_live(s, 0, session_id=sess_a, **fixed("running", [])) for s in steps_a
    ]
    other = bus.replay_then_live(step_b, 0, session_id=sess_b, **fixed("running", []))
    for it in [*streams, other]:
        await nxt(it)  # status; now subscribed and waiting live
    waits = [asyncio.ensure_future(anext(it)) for it in streams]
    other_wait = asyncio.ensure_future(anext(other))
    await asyncio.sleep(0)
    assert bus.close_session_streams(sess_a) == 2
    for w in waits:
        with pytest.raises(StopAsyncIteration):
            await within(w)
    assert bus.channel_count() == 1 and bus.subscriber_count(step_b) == 1
    bus.publish(step_b, started(1))
    assert (await within(other_wait)).type == "tool_started"
    await other.aclose()
    assert bus.channel_count() == 0


async def test_slow_subscriber_is_dropped_and_never_blocks() -> None:
    bus = EventBus(backlog=3)
    step, sess = uuid4(), uuid4()
    slow = bus.replay_then_live(step, 0, session_id=sess, **fixed("running", []))
    await nxt(slow)
    fast = bus.replay_then_live(step, 0, session_id=sess, **fixed("running", []))
    await nxt(fast)
    fast_got: list[Any] = []

    async def drain() -> None:
        async for e in fast:
            fast_got.append(e)

    reader = asyncio.create_task(drain())
    for k in range(1, 11):
        bus.publish(step, started(k))  # synchronous: returns without waiting for anyone
        await asyncio.sleep(0)  # the fast reader keeps up; the slow one never reads
    assert bus.subscriber_count(step) == 1
    bus.publish(step, node_event(error_node(step, sess), node_seq(11)))
    await within(reader)
    assert len(fast_got) == 11 and fast_got[-1].type == "error"
    # The slow stream gets its backlog, then ends (the client resumes by Last-Event-ID).
    rest = await collect(slow)
    assert [e.seq for e in rest] == [1, 3, 5]
    assert bus.channel_count() == 0


async def test_last_event_id_beyond_the_end_still_ends_a_live_stream() -> None:
    bus = EventBus()
    step, sess = uuid4(), uuid4()
    it = bus.replay_then_live(step, 10**9, session_id=sess, **fixed("running", []))
    assert seqs([await nxt(it)]) == [("status", 0)]
    pending = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0)
    bus.publish(step, started(1))  # deduped: below last_seq
    bus.publish(step, node_event(error_node(step, sess), node_seq(1)))  # final: always passes
    assert seqs([await within(pending)]) == [("error", 2)]
    with pytest.raises(StopAsyncIteration):
        await nxt(it)
    assert bus.channel_count() == 0


async def test_negative_last_event_id_is_clamped() -> None:
    bus = EventBus()
    step, sess = uuid4(), uuid4()
    nodes = [tool_node(step, sess), error_node(step, sess)]
    events = await collect(bus.replay_then_live(step, -5, session_id=sess, **fixed("error", nodes)))
    assert seqs(events) == [("status", 0), ("tool_done", 2), ("error", 4)]


async def test_publish_without_subscribers_is_a_noop() -> None:
    bus = EventBus()
    bus.publish(uuid4(), started(1))
    assert bus.channel_count() == 0


async def test_session_mismatch_is_refused() -> None:
    bus = EventBus()
    step = uuid4()
    first = bus.replay_then_live(step, 0, session_id=uuid4(), **fixed("running", []))
    await nxt(first)
    second = bus.replay_then_live(step, 0, session_id=uuid4(), **fixed("running", []))
    with pytest.raises(ValueError, match="another session"):
        await nxt(second)
    await first.aclose()
    assert bus.channel_count() == 0


# --- against a real step -----------------------------------------------------------------


class Scripted:
    name = "scripted"
    model = "scripted"

    def __init__(self) -> None:
        self.turns: asyncio.Queue[Any] = asyncio.Queue()
        self.waiting: asyncio.Queue[TurnSchema] = asyncio.Queue()

    async def next_turn(self, messages: list[Message], schema: TurnSchema) -> Any:
        self.waiting.put_nowait(schema)
        return await self.turns.get()


ANSWER = AnswerTurn(
    action="answer",
    answer=Waypoint(
        title="Overview",
        summary="It is a demo.",
        key_points=["one", "two"],
        code_refs=[],
        next_moves=[],
        confidence="medium",
    ),
)


def list_dir() -> ToolTurn:
    return ToolTurn(action="tool", tool="list_dir", args={"path": "."}, why="look")


async def setup_step(
    db: AsyncSession, make_user: MakeUser, tmp_path: Path
) -> tuple[StepContext, Scripted]:
    owner = await make_user()
    session, _root = await repo.create_session(
        db,
        owner_id=owner,
        installation_id=1,
        repo_id=4243,
        repo_full_name="fixture/demo",
        ref="main",
        commit_sha="b" * 40,
        private=False,
        model="mock",
        status="ready",
    )
    step = await repo.create_step(
        db,
        session_id=session.id,
        parent_node_id=None,
        steered_by=owner,
        wedge="explain",
        instruction="overview",
        mode="safe",
        provider="mock",
        model="mock",
    )
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "a.txt").write_text("a\n")
    provider = Scripted()
    ctx = StepContext(
        step=step,
        session=session,
        mode="safe",
        allowlist=(),
        model="mock",
        provider=provider,
        snapshot_root=snap,
    )
    return ctx, provider


def db_loaders(db: AsyncSession, step_id: UUID) -> dict[str, Any]:
    async def load_status() -> StepStatus:
        status = (
            await db.execute(select(StepRow.status).where(StepRow.id == step_id))
        ).scalar_one()
        await db.commit()
        return status  # type: ignore[no-any-return]

    async def load_nodes() -> list[GraphNode]:
        return await repo.step_nodes(db, step_id)

    return {"load_status": load_status, "load_nodes": load_nodes}


async def test_mid_step_subscriber_gets_replay_then_live(
    db: AsyncSession, make_user: MakeUser, tmp_path: Path
) -> None:
    ctx, provider = await setup_step(db, make_user, tmp_path)
    step_id, sess_id = ctx.step.id, ctx.session.id
    bus = EventBus()
    # An early subscriber sees everything live (its "DB read" is fixed: the test has one
    # DB session, which must not be used concurrently with the step).
    early = bus.replay_then_live(step_id, 0, session_id=sess_id, **fixed("queued", []))
    assert seqs([await nxt(early)]) == [("status", 0)]
    early_got: list[Any] = []

    async def drain_early() -> None:
        async for e in early:
            early_got.append(e)

    task = asyncio.create_task(run_step(SameSession(db).maker(), ctx, bus))
    early_reader = asyncio.create_task(drain_early())
    await provider.waiting.get()
    provider.turns.put_nowait(list_dir())
    await provider.waiting.get()  # node 1 committed and published; the loop is idle

    # A late subscriber joins mid-step (the DB is idle while the loop waits on the model).
    late = bus.replay_then_live(step_id, 0, session_id=sess_id, **db_loaders(db, step_id))
    late_got = [await nxt(late), await nxt(late)]
    assert seqs(late_got) == [("status", 0), ("tool_done", 2)]
    assert late_got[0].status == "running"

    provider.turns.put_nowait(list_dir())
    await provider.waiting.get()
    provider.turns.put_nowait(ANSWER)
    final = await within(task)
    late_got += await collect(late)
    await within(early_reader)

    assert seqs(late_got) == [
        ("status", 0),
        ("tool_done", 2),
        ("tool_started", 3),
        ("tool_done", 4),
        ("waypoint", 6),
    ]
    assert seqs(early_got) == [
        ("tool_started", 1),
        ("tool_done", 2),
        ("tool_started", 3),
        ("tool_done", 4),
        ("waypoint", 6),
    ]
    assert late_got[-1].node.id == final.id
    assert bus.channel_count() == 0

    # Replay after the fact: identical seq and payload for every node event.
    replay = await collect(
        bus.replay_then_live(step_id, 0, session_id=sess_id, **db_loaders(db, step_id))
    )
    assert replay[0] == StatusEvent(seq=0, status="done")
    live_nodes = [e for e in early_got if e.type != "tool_started"]
    assert [e.model_dump() for e in replay[1:]] == [e.model_dump() for e in live_nodes]

    # Last-Event-ID resume.
    resumed = await collect(
        bus.replay_then_live(step_id, 3, session_id=sess_id, **db_loaders(db, step_id))
    )
    assert seqs(resumed) == [("status", 0), ("tool_done", 4), ("waypoint", 6)]


async def test_slow_subscriber_does_not_block_a_step(
    db: AsyncSession, make_user: MakeUser, tmp_path: Path
) -> None:
    ctx, provider = await setup_step(db, make_user, tmp_path)
    bus = EventBus(backlog=1)
    slow = bus.replay_then_live(
        ctx.step.id, 0, session_id=ctx.session.id, **db_loaders(db, ctx.step.id)
    )
    await nxt(slow)  # subscribed; it reads nothing more during the step
    for turn in (list_dir(), list_dir(), ANSWER):
        provider.turns.put_nowait(turn)
    final = await asyncio.wait_for(run_step(SameSession(db).maker(), ctx, bus), timeout=10)
    assert final.kind == "waypoint"
    assert bus.subscriber_count(ctx.step.id) == 0
    # Dropped after one queued live event; resuming its read replays the rest from the DB.
    assert [e.seq for e in await collect(slow)] == [2, 4, 6]
    assert bus.channel_count() == 0

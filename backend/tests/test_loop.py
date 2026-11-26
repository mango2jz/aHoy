"""Step loop (T6): mock-scripted steps against real DB rows and a temp snapshot."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.harness import loop as loop_mod
from ahoy.harness.context import BLOCKED_NOTICE
from ahoy.harness.events import EventBus
from ahoy.harness.loop import (
    ERR_CANCELLED,
    ERR_INTERNAL,
    ERR_INVALID_TWICE,
    ERR_PROVIDER,
    FORCE_ANSWER_NOTE,
    SHELL_UNAVAILABLE,
    StepContext,
    normalize_waypoint,
    run_step,
    validate_waypoint,
)
from ahoy.harness.schemas import (
    AnswerTurn,
    AnyTurn,
    CodeRef,
    GraphNode,
    NextMove,
    Session,
    Step,
    ToolCall,
    ToolTurn,
    TurnSchema,
    Waypoint,
)
from ahoy.providers.base import Message, ProviderError
from ahoy.providers.mock import MockProvider
from ahoy.store import repo
from ahoy.store.models import StepRow
from ahoy.store.repo import StepStateError
from conftest import SameSession

MakeUser = Callable[[], Awaitable[UUID]]
APP_PY = "".join(f"line {i}\n" for i in range(1, 11))  # 10 lines


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snap"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP_PY)
    (root / "README.md").write_text("# demo\n")
    (root / "config").mkdir()
    (root / "config" / "dev.pem").write_text("secret\n")
    (root / "bin.dat").write_bytes(b"a\x00b\n")
    return root


def tool(name: str, **args: Any) -> ToolTurn:
    return ToolTurn(action="tool", tool=name, args=args, why=f"look at {name}")  # type: ignore[arg-type]


def waypoint(**over: Any) -> Waypoint:
    base: dict[str, Any] = {
        "title": "How the app starts",
        "summary": "The app prints ten lines.",
        "key_points": ["It is small.", "It prints."],
        "code_refs": [CodeRef(path="src/app.py", start_line=1, end_line=3, note="start")],
        "next_moves": [NextMove(label="Deeper", instruction="Go deeper")],
        "confidence": "high",
    }
    return Waypoint(**(base | over))


def answer(**over: Any) -> AnswerTurn:
    return AnswerTurn(action="answer", answer=waypoint(**over))


async def new_step(db: AsyncSession, make_user: MakeUser) -> tuple[Session, Step]:
    owner = await make_user()
    session, _root = await repo.create_session(
        db,
        owner_id=owner,
        installation_id=1,
        repo_id=4242,
        repo_full_name="fixture/demo",
        ref="main",
        commit_sha="a" * 40,
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
        instruction="Give me an overview",
        mode="safe",
        provider="mock",
        model="mock",
    )
    return session, step


def make_ctx(session: Session, step: Step, provider: Any, snap: Path, **kw: Any) -> StepContext:
    return StepContext(
        step=step,
        session=session,
        mode="safe",
        allowlist=(),
        model="mock",
        provider=provider,
        snapshot_root=snap,
        **kw,
    )


class Recorder(EventBus):
    """A bus that also records everything published."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[Any] = []

    def publish(self, step_id: UUID, event: Any) -> None:
        self.published.append(event)
        super().publish(step_id, event)


async def step_status(db: AsyncSession, step_id: UUID) -> str:
    status = (await db.execute(select(StepRow.status).where(StepRow.id == step_id))).scalar_one()
    await db.commit()
    return status


def finals(nodes: list[GraphNode]) -> list[GraphNode]:
    return [n for n in nodes if n.kind in ("waypoint", "error")]


@pytest.fixture(autouse=True)
def _fresh_map_cache() -> None:
    loop_mod.clear_repo_map_cache()


# --- validate_waypoint (pure) ------------------------------------------------------------


def test_validate_waypoint_accepts_a_good_answer() -> None:
    assert validate_waypoint(waypoint()) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"title": "  "}, "title is empty"),
        ({"summary": ""}, "summary is empty"),
        ({"key_points": []}, "key_points is empty"),
        ({"key_points": ["a", " "]}, "a key point is empty"),
        ({"next_moves": [NextMove(label="", instruction="y")]}, "next move 1 needs"),
        ({"next_moves": [NextMove(label="x", instruction=" ")]}, "next move 1 needs"),
        ({"code_refs": [CodeRef(path=" ", start_line=1, end_line=1, note="n")]}, "code ref 1"),
        ({"code_refs": [CodeRef(path="a.py", start_line=1, end_line=1, note="")]}, "code ref 1"),
    ],
)
def test_validate_waypoint_problems(over: dict[str, Any], needle: str) -> None:
    problems = validate_waypoint(waypoint(**over))
    assert any(needle in p for p in problems), problems


def test_limits_are_clamped_or_accepted(caplog: pytest.LogCaptureFixture) -> None:
    long_title = "one two three four five six seven eight nine"
    wp = waypoint(
        title=long_title,
        summary="w " * 151,
        key_points=["only one"],
        next_moves=[NextMove(label=f"m{i}", instruction="go") for i in range(6)],
    )
    wp = wp.model_copy(update={"key_points": [f"k{i}" for i in range(7)]})
    assert validate_waypoint(wp) == []
    with caplog.at_level("WARNING", logger="ahoy.loop"):
        out = normalize_waypoint(wp)
    assert out.key_points == ["k0", "k1", "k2", "k3", "k4"]
    assert [m.label for m in out.next_moves] == ["m0", "m1", "m2", "m3"]
    assert out.title == long_title
    assert "title 9 words, summary 151 words" in caplog.text
    assert "one two" not in caplog.text
    assert validate_waypoint(waypoint(key_points=["just one"])) == []


def test_model_facing_schemas_do_not_ask_for_valid() -> None:
    for schema in (AnyTurn, AnswerTurn):
        assert '"valid"' not in json.dumps(schema.model_json_schema())
    assert "valid" in CodeRef.model_json_schema(mode="serialization")["properties"]


# --- steps against the DB ---------------------------------------------------------------


async def test_happy_path_tools_then_answer(
    db: AsyncSession, make_user: MakeUser, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider(
        [tool("list_dir", path="."), tool("read_file", path="src/app.py"), answer()]
    )
    order: list[str] = []
    real_append = repo.append_node

    async def append(db_: AsyncSession, node: GraphNode) -> GraphNode:
        stored = await real_append(db_, node)
        order.append(f"insert {stored.id}")
        return stored

    monkeypatch.setattr(repo, "append_node", append)
    bus = Recorder()
    real_publish = bus.publish

    def publish(step_id: UUID, event: Any) -> None:
        node = getattr(event, "node", None)
        if node is not None:
            order.append(f"publish {node.id}")
        real_publish(step_id, event)

    monkeypatch.setattr(bus, "publish", publish)

    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), bus
    )

    nodes = await repo.step_nodes(db, step.id)
    assert [n.kind for n in nodes] == ["tool", "tool", "waypoint"]
    assert final.id == nodes[-1].id and final.kind == "waypoint"
    assert all(n.step_id == step.id for n in nodes)
    path = await repo.root_path(db, final.id)
    assert [n.kind for n in path] == ["root", "tool", "tool", "waypoint"]
    assert nodes[1].tool is not None and "3: line 3" in nodes[1].tool.output
    assert final.steer is not None and final.steer.instruction == "Give me an overview"
    assert final.mode == "safe"
    assert await step_status(db, step.id) == "done"
    # Each node is inserted before its event is published.
    tool_ids = [str(n.id) for n in nodes[:2]]
    for nid in tool_ids:
        assert order.index(f"insert {nid}") < order.index(f"publish {nid}")
    assert [(e.type, e.seq) for e in bus.published] == [
        ("tool_started", 1),
        ("tool_done", 2),
        ("tool_started", 3),
        ("tool_done", 4),
        ("waypoint", 6),
    ]
    # The second model call saw the first tool result.
    second_messages = provider.calls[1][0]
    assert any("src/app.py" in m.text for m in second_messages if m.role == "user")


async def test_runaway_forces_answer_with_tools_removed(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider([tool("list_dir", path=".")] * 15 + [answer()])
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.kind == "waypoint"
    schemas: list[TurnSchema] = [s for _m, s in provider.calls]
    assert len(schemas) == 16
    assert all(s is AnyTurn for s in schemas[:15])
    assert schemas[15] is AnswerTurn
    assert provider.calls[15][0][-1].text == FORCE_ANSWER_NOTE
    nodes = await repo.step_nodes(db, step.id)
    assert [n.kind for n in nodes].count("tool") == 15


async def test_invalid_then_valid_answer(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider([answer(key_points=[]), answer()])
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.kind == "waypoint"
    assert provider.calls[1][1] is AnswerTurn
    retry = provider.calls[1][0]
    assert retry[-2].role == "assistant" and '"key_points":[]' in retry[-2].text
    assert "key_points is empty" in retry[-1].text
    assert await step_status(db, step.id) == "done"


async def test_malformed_reply_is_retried(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    class Malformed(MockProvider):
        async def next_turn(self, messages: list[Message], schema: TurnSchema) -> Any:
            if not self.calls:
                self.calls.append((list(messages), schema))
                raise ProviderError("not a valid turn", raw='{"action": "answer"')
            return await super().next_turn(messages, schema)

    session, step = await new_step(db, make_user)
    provider = Malformed([answer()])
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.kind == "waypoint"
    assert provider.calls[1][0][-2].text == '{"action": "answer"'


async def test_invalid_twice_is_an_error_node(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider([answer(title=""), answer(summary="")])
    bus = Recorder()
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), bus
    )
    assert final.kind == "error" and final.error == ERR_INVALID_TWICE
    assert await step_status(db, step.id) == "error"
    assert [(e.type, e.seq) for e in bus.published] == [("error", 2)]


async def test_bad_code_refs_are_flagged(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    refs = [
        CodeRef(path="src/app.py", start_line=2, end_line=10, note="ok"),
        CodeRef(path="src/missing.py", start_line=1, end_line=1, note="missing"),
        CodeRef(path="config/dev.pem", start_line=1, end_line=1, note="denylisted"),
        CodeRef(path="src/app.py", start_line=5, end_line=11, note="past the end"),
        CodeRef(path="src/app.py", start_line=0, end_line=2, note="zero"),
        CodeRef(path="src/app.py", start_line=4, end_line=3, note="reversed"),
        CodeRef(path="src", start_line=1, end_line=1, note="a directory"),
        CodeRef(path="../outside.py", start_line=1, end_line=1, note="escape"),
        CodeRef(path="bin.dat", start_line=1, end_line=1, note="binary"),
    ]
    session, step = await new_step(db, make_user)
    provider = MockProvider([answer(code_refs=refs)])
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.waypoint is not None
    assert [r.valid for r in final.waypoint.code_refs] == [
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    stored = (await repo.step_nodes(db, step.id))[-1]
    assert stored.waypoint is not None
    assert [r.valid for r in stored.waypoint.code_refs][:2] == [True, False]


async def test_model_cannot_mark_its_own_ref_valid(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    ref = CodeRef(path="nope.py", start_line=1, end_line=1, note="x", valid=True)
    session, step = await new_step(db, make_user)
    provider = MockProvider([answer(code_refs=[ref])])
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.waypoint is not None and final.waypoint.code_refs[0].valid is False


@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        ([tool("list_dir", path=".")], ERR_PROVIDER),  # script exhausted: ProviderError
    ],
)
async def test_provider_exception_ends_in_one_error_node(
    db: AsyncSession,
    make_user: MakeUser,
    snapshot: Path,
    turns: list[Any],
    expected: str,
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider(turns)
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.kind == "error" and final.error == expected
    nodes = await repo.step_nodes(db, step.id)
    assert [n.kind for n in nodes] == ["tool", "error"]
    assert len(finals(nodes)) == 1
    assert await step_status(db, step.id) == "error"


async def test_unexpected_exception_text_never_reaches_the_node(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    class Boom:
        name = "boom"
        model = "boom"

        async def next_turn(self, messages: list[Message], schema: TurnSchema) -> Any:
            raise RuntimeError("token=ghs_SECRETSECRET")

    session, step = await new_step(db, make_user)
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, Boom(), snapshot), EventBus()
    )
    assert final.kind == "error" and final.error == ERR_INTERNAL
    assert "SECRET" not in final.model_dump_json()
    assert len(finals(await repo.step_nodes(db, step.id))) == 1


async def test_tool_refusals_are_tool_output(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider(
        [
            tool("read_file", path="config/dev.pem"),
            tool("read_file", path="nope.py"),
            tool("read_file", path=3),
            tool("search", query="line 7", extra=True),
            tool("search", query="line 7"),
            answer(),
        ]
    )
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
    )
    assert final.kind == "waypoint"
    outputs = [n.tool.output for n in (await repo.step_nodes(db, step.id)) if n.tool]
    assert outputs[0].startswith("error: path refused (denylisted)")
    assert outputs[1].startswith("error: no such file")
    assert outputs[2].startswith("error: invalid arguments for read_file")
    assert outputs[3].startswith("error: invalid arguments for search")
    assert "src/app.py" in outputs[4]
    assert "secret" not in "".join(outputs)


async def test_shell_turn_becomes_blocked_node(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    provider = MockProvider([tool("shell", command="ls -la"), answer()])
    bus = Recorder()
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, provider, snapshot), bus
    )
    assert final.kind == "waypoint"
    nodes = await repo.step_nodes(db, step.id)
    assert nodes[0].kind == "blocked" and nodes[0].blocked is not None
    assert nodes[0].blocked.command == "ls -la"
    assert nodes[0].blocked.reason == SHELL_UNAVAILABLE
    assert any(BLOCKED_NOTICE in m.text for m in provider.calls[1][0])
    assert [e.type for e in bus.published] == ["tool_started", "blocked", "waypoint"]


class Scripted:
    """A provider the test feeds one turn at a time; `waiting` fires at each call."""

    name = "scripted"
    model = "scripted"

    def __init__(self) -> None:
        self.turns: asyncio.Queue[Any] = asyncio.Queue()
        self.waiting: asyncio.Queue[TurnSchema] = asyncio.Queue()

    async def next_turn(self, messages: list[Message], schema: TurnSchema) -> Any:
        self.waiting.put_nowait(schema)
        return await self.turns.get()


async def test_cancellation_closes_with_error_then_reraises(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    provider = Scripted()
    bus = Recorder()
    task = asyncio.create_task(
        run_step(SameSession(db).maker(), make_ctx(session, step, provider, snapshot), bus)
    )
    await provider.waiting.get()
    provider.turns.put_nowait(tool("list_dir", path="."))
    await provider.waiting.get()  # the tool node is committed; the loop waits again
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    nodes = await repo.step_nodes(db, step.id)
    assert [n.kind for n in nodes] == ["tool", "error"]
    assert nodes[-1].error == ERR_CANCELLED
    assert await step_status(db, step.id) == "error"
    assert [(e.type, e.seq) for e in bus.published][-1] == ("error", 4)


async def test_repo_map_is_cached(db: AsyncSession, make_user: MakeUser, snapshot: Path) -> None:
    calls = 0
    real = loop_mod.repo_map

    def counting(*a: Any) -> str:
        nonlocal calls
        calls += 1
        return real(*a)

    loop_mod.repo_map = counting  # type: ignore[assignment]
    try:
        session, step = await new_step(db, make_user)
        provider = MockProvider([tool("list_dir", path=".")] * 3 + [answer()])
        await run_step(
            SameSession(db).maker(), make_ctx(session, step, provider, snapshot), EventBus()
        )
    finally:
        loop_mod.repo_map = real
    assert calls == 1
    assert "README.md" in provider.calls[0][0][1].text


# --- review fixes: start, close and cancellation edges -----------------------------------


async def root_id(db: AsyncSession, session: Session) -> UUID:
    return next(n.id for n in await repo.load_nodes(db, session.id) if n.kind == "root")


async def test_start_db_error_on_overview_closes_with_one_error_node(
    db: AsyncSession, make_user: MakeUser, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(db_: AsyncSession, step_id: UUID) -> Step:
        raise OperationalError("UPDATE steps", {}, Exception("connection lost"))

    monkeypatch.setattr(repo, "start_step", broken)
    session, step = await new_step(db, make_user)
    bus = Recorder()
    final = await run_step(
        SameSession(db).maker(), make_ctx(session, step, MockProvider([]), snapshot), bus
    )
    assert final.kind == "error" and final.error == ERR_INTERNAL
    assert final.parent_id == await root_id(db, session)
    assert [n.kind for n in await repo.step_nodes(db, step.id)] == ["error"]
    assert await step_status(db, step.id) == "error"
    assert [(e.type, e.seq) for e in bus.published] == [("error", 2)]


async def test_cancel_during_start_on_overview_closes_with_one_error_node(
    db: AsyncSession, make_user: MakeUser, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def hanging(db_: AsyncSession, step_id: UUID) -> Step:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(repo, "start_step", hanging)
    session, step = await new_step(db, make_user)
    bus = Recorder()
    task = asyncio.create_task(
        run_step(SameSession(db).maker(), make_ctx(session, step, MockProvider([]), snapshot), bus)
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    nodes = await repo.step_nodes(db, step.id)
    assert [(n.kind, n.error) for n in nodes] == [("error", ERR_CANCELLED)]
    assert nodes[0].parent_id == await root_id(db, session)
    assert await step_status(db, step.id) == "error"
    assert [(e.type, e.seq) for e in bus.published] == [("error", 2)]


async def test_step_started_elsewhere_is_left_untouched(
    db: AsyncSession, make_user: MakeUser, snapshot: Path
) -> None:
    session, step = await new_step(db, make_user)
    await repo.start_step(db, step.id)  # another runner owns it
    bus = Recorder()
    with pytest.raises(StepStateError):
        await run_step(
            SameSession(db).maker(),
            make_ctx(session, step, MockProvider([answer()]), snapshot),
            bus,
        )
    assert await repo.step_nodes(db, step.id) == []
    assert await step_status(db, step.id) == "running"
    assert bus.published == []


async def test_close_at_tip_reads_parent_and_position_in_the_transaction(
    db: AsyncSession, make_user: MakeUser
) -> None:
    session, step = await new_step(db, make_user)
    await repo.start_step(db, step.id)
    root = await root_id(db, session)
    first = await repo.append_node(
        db,
        GraphNode(
            id=uuid4(),
            session_id=session.id,
            step_id=step.id,
            parent_id=root,
            kind="tool",
            tool=ToolCall(name="list_dir", args={}, output="x"),
            model="mock",
            created_at=datetime.now(UTC),
        ),
    )
    draft = GraphNode(
        id=uuid4(),
        session_id=session.id,
        step_id=step.id,
        parent_id=None,  # ignored: the tip is read from the DB
        kind="error",
        error="x",
        model="mock",
        created_at=datetime.now(UTC),
    )
    closed, stored, k = await repo.close_step_at_tip(db, step.id, "error", draft)
    assert (closed.status, stored.parent_id, k) == ("error", first.id, 2)


async def test_cancel_after_append_commit_keeps_the_chain(
    db: AsyncSession, make_user: MakeUser, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled after a tool node committed but before the loop recorded it."""
    committed = asyncio.Event()
    real_append = repo.append_node

    async def append_then_hang(db_: AsyncSession, node: GraphNode) -> GraphNode:
        await real_append(db_, node)
        committed.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(repo, "append_node", append_then_hang)
    session, step = await new_step(db, make_user)
    bus = Recorder()
    provider = MockProvider([tool("list_dir", path=".")])
    task = asyncio.create_task(
        run_step(SameSession(db).maker(), make_ctx(session, step, provider, snapshot), bus)
    )
    await asyncio.wait_for(committed.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    nodes = await repo.step_nodes(db, step.id)
    assert [n.kind for n in nodes] == ["tool", "error"]
    assert nodes[1].parent_id == nodes[0].id  # not a sibling of the tool node
    assert [(e.type, e.seq) for e in bus.published] == [("tool_started", 1), ("error", 4)]
    replay = await asyncio.wait_for(
        _collect(
            EventBus().replay_then_live(
                step.id,
                0,
                session_id=session.id,
                load_status=lambda: _status(db, step.id),
                load_nodes=lambda: repo.step_nodes(db, step.id),
            )
        ),
        5,
    )
    assert [(e.type, e.seq) for e in replay] == [("status", 0), ("tool_done", 2), ("error", 4)]


async def _status(db: AsyncSession, step_id: UUID) -> Any:
    return await step_status(db, step_id)


async def _collect(it: Any) -> list[Any]:
    return [e async for e in it]


async def test_cancel_during_close_lets_the_close_finish(
    db: AsyncSession, make_user: MakeUser, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close is shielded: cancelling mid-close still lands the waypoint, once."""
    entered, gate = asyncio.Event(), asyncio.Event()
    real_close = repo.close_step_at_tip

    async def slow_close(db_: AsyncSession, *a: Any) -> Any:
        entered.set()
        await gate.wait()
        return await real_close(db_, *a)

    monkeypatch.setattr(repo, "close_step_at_tip", slow_close)
    session, step = await new_step(db, make_user)
    bus = Recorder()
    task = asyncio.create_task(
        run_step(
            SameSession(db).maker(),
            make_ctx(session, step, MockProvider([answer()]), snapshot),
            bus,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    nodes = await repo.step_nodes(db, step.id)
    assert [n.kind for n in nodes] == ["waypoint"]
    assert await step_status(db, step.id) == "done"
    assert [(e.type, e.seq) for e in bus.published] == [("waypoint", 2)]


async def test_double_cancel_during_the_cancel_close_still_one_error_node(
    db: AsyncSession, make_user: MakeUser, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cancel while the cancel handler's error close runs: the shielded close
    still lands, once."""
    in_provider, entered, gate, landed = (asyncio.Event() for _ in range(4))
    real_close = repo.close_step_at_tip

    async def slow_close(db_: AsyncSession, *a: Any) -> Any:
        entered.set()
        await gate.wait()
        result = await real_close(db_, *a)
        landed.set()
        return result

    class Hanging:
        name = "mock"
        model = "mock"

        async def next_turn(self, messages: list[Message], schema: TurnSchema) -> Any:
            in_provider.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(repo, "close_step_at_tip", slow_close)
    session, step = await new_step(db, make_user)
    bus = Recorder()
    task = asyncio.create_task(
        run_step(SameSession(db).maker(), make_ctx(session, step, Hanging(), snapshot), bus)
    )
    await asyncio.wait_for(in_provider.wait(), 5)
    task.cancel()
    await asyncio.wait_for(entered.wait(), 5)  # the cancel handler is closing the step
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    gate.set()
    await asyncio.wait_for(landed.wait(), 5)
    await asyncio.sleep(0)  # let the close task publish after its commit
    nodes = await repo.step_nodes(db, step.id)
    assert [(n.kind, n.error) for n in nodes] == [("error", ERR_CANCELLED)]
    assert await step_status(db, step.id) == "error"
    assert [(e.type, e.seq) for e in bus.published] == [("error", 2)]

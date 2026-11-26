"""T7: steer API, step events (SSE) and the step runner, on committed rows.

Requests, the runner and the SSE streams run concurrently, so this file uses the real
sessionmaker on this run's test database (not the rolled-back shared session) and removes
every row it made afterwards. Mock provider and mock GitHub throughout. Steps are held at
explicit gates (asyncio events); the only timer is the SSE re-check interval.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from ahoy.api.events import parse_last_event_id
from ahoy.api.sessions import Preparer
from ahoy.app import create_app
from ahoy.auth import crypto
from ahoy.auth.sessions import AuthState
from ahoy.github.mock import INSTALLATION_ID, MockGitHub
from ahoy.github.repo_cache import RepoCache
from ahoy.harness.loop import ERR_CANCELLED, ERR_GITHUB
from ahoy.harness.runner import OVERVIEW_INSTRUCTION, OVERVIEW_WEDGE, StepRunner
from ahoy.harness.schemas import (
    AnswerTurn,
    GraphNode,
    Session,
    Step,
    ToolTurn,
    TurnSchema,
    User,
)
from ahoy.providers.base import Message
from ahoy.providers.mock import MockProvider, demo_script
from ahoy.store import auth as auth_store
from ahoy.store import repo
from ahoy.store.db import make_engine, make_sessionmaker
from ahoy.store.models import SessionRow, UserRow

pytestmark = pytest.mark.db

ORIGIN = "http://127.0.0.1:5173"
CSRF = {"Origin": ORIGIN, "X-Ahoy-CSRF": "1"}
DEMO = 7001  # private: alice and bob can read it, mallory cannot
PUBLIC = 7002
SHA = "0" * 40
WAIT = 20  # seconds: a bound on every wait, never a pace
USERS = ("alice", "bob", "mallory")

Maker = async_sessionmaker[AsyncSession]


# --- scripted providers ---------------------------------------------------------------


@dataclass
class Gate:
    """A pause in a script: `reached` is set when the step gets there, then it waits."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class Scripted:
    name = "mock"
    model = "mock"

    def __init__(self, items: list[ToolTurn | AnswerTurn | Gate]) -> None:
        self.items = list(items)
        self.calls: list[list[Message]] = []

    async def next_turn(self, messages: list[Message], schema: TurnSchema) -> ToolTurn | AnswerTurn:
        self.calls.append(list(messages))
        while self.items and isinstance(self.items[0], Gate):
            gate = self.items.pop(0)
            assert isinstance(gate, Gate)
            gate.reached.set()
            await gate.release.wait()
        turn = self.items.pop(0)
        assert not isinstance(turn, Gate)
        return turn


def list_dir() -> ToolTurn:
    return ToolTurn(action="tool", tool="list_dir", args={"path": "."}, why="look")


def answer(title: str) -> AnswerTurn:
    return AnswerTurn.model_validate(
        {
            "action": "answer",
            "answer": {
                "title": title,
                "summary": f"Summary of {title}.",
                "key_points": [f"{title} one.", f"{title} two."],
                "code_refs": [],
                "next_moves": [],
                "confidence": "high",
            },
        }
    )


class Providers:
    """Per-instruction scripts for the runner; anything else gets the demo script."""

    def __init__(self) -> None:
        self.scripts: dict[str, Scripted] = {}

    def add(self, instruction: str, *items: ToolTurn | AnswerTurn | Gate) -> Scripted:
        self.scripts[instruction] = Scripted(list(items))
        return self.scripts[instruction]

    def __call__(self, step: Step) -> Any:
        return self.scripts.pop(step.instruction, None) or MockProvider(demo_script())


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture
async def sm(test_database: URL) -> AsyncIterator[Maker]:
    engine = make_engine(test_database, poolclass=NullPool)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@dataclass
class Env:
    app: FastAPI
    sm: Maker
    providers: Providers
    stack: AsyncExitStack

    @property
    def runner(self) -> StepRunner:
        r: StepRunner = self.app.state.runner
        return r

    @property
    def gh(self) -> MockGitHub:
        gh: MockGitHub = self.app.state.github_user
        return gh

    async def client(self, name: str) -> httpx.AsyncClient:
        client = await self.stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
            )
        )
        resp = await client.get("/auth/mock/login", params={"user": name})
        assert resp.status_code == 303, resp.text
        return client

    async def user(self, client: httpx.AsyncClient) -> User:
        async with self.sm() as db:
            user = await auth_store.get_user(db, UUID((await client.get("/api/me")).json()["id"]))
        assert user is not None
        return user

    async def session(
        self, owner: User, *, shared: bool = False, status: str = "ready", repo_id: int = DEMO
    ) -> Session:
        async with self.sm() as db:
            session, _root = await repo.create_session(
                db,
                owner_id=owner.id,
                installation_id=INSTALLATION_ID,
                repo_id=repo_id,
                repo_full_name="fixture/demo",
                ref="main",
                commit_sha=SHA,
                private=True,
                model="mock",
                status=status,  # type: ignore[arg-type]
            )
            if shared:
                await db.execute(
                    update(SessionRow).where(SessionRow.id == session.id).values(shared=True)
                )
                await db.commit()
                session = session.model_copy(update={"shared": True})
        return session

    async def overview(self, session: Session, owner: User) -> GraphNode:
        """Run the session's Overview step (demo script) to its waypoint."""
        async with self.sm() as db:
            step = await self.runner.admit(
                db,
                session,
                owner,
                parent_node_id=None,
                wedge=OVERVIEW_WEDGE,
                instruction=OVERVIEW_INSTRUCTION,
            )
        await self.runner.drain()
        return (await self.nodes(step.id))[-1]

    async def nodes(self, step_id: UUID) -> list[GraphNode]:
        async with self.sm() as db:
            return await repo.step_nodes(db, step_id)

    async def step(self, step_id: UUID) -> Step:
        async with self.sm() as db:
            step = await repo.get_step(db, step_id)
        assert step is not None
        return step


async def seed_snapshot(cache: RepoCache) -> None:
    async def fetch(dest: Path) -> None:
        (dest / "README.md").write_text("# demo\n")
        (dest / "src").mkdir()
        (dest / "src" / "app.py").write_text("print('hi')\n")

    async with cache.lease(DEMO, SHA, fetch):
        pass


async def _cleanup(sm: Maker) -> None:
    async with sm() as db:
        users = (
            (await db.execute(select(UserRow.id).where(UserRow.github_login.in_(USERS))))
            .scalars()
            .all()
        )
        for uid in users:
            owned = [s.id for s in await repo.list_owned_sessions(db, uid)]
            if owned:
                await repo.purge(db, uid, owned)
        await db.execute(text("DELETE FROM users WHERE github_login = ANY(:l)"), {"l": list(USERS)})
        await db.commit()


@pytest.fixture
async def env(sm: Maker, tmp_path: Path) -> AsyncIterator[Env]:
    from ahoy.config import Settings

    settings = Settings(
        _env_file=None,
        ahoy_env="test",
        database_url=None,
        app_origin=ORIGIN,
        token_encryption_key=crypto.generate_key(),
        repo_cache_dir=str(tmp_path / "cache"),
    )
    app = create_app(settings, "mock", "mock")
    auth: AuthState = app.state.auth
    auth.sessionmaker = sm  # get_db, the preparer and the runner all use it
    providers = Providers()
    app.state.runner.provider_for = providers
    await _cleanup(sm)
    await seed_snapshot(app.state.repo_cache)
    try:
        async with AsyncExitStack() as stack:
            yield Env(app, sm, providers, stack)
    finally:
        await app.state.preparer.shutdown(WAIT)
        await app.state.runner.shutdown()
        await _cleanup(sm)


# --- SSE helpers ----------------------------------------------------------------------


@dataclass
class Sse:
    id: str
    event: str
    data: dict[str, Any]


def parse_sse(body: str) -> list[Sse]:
    out = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if "event" in fields:
            out.append(Sse(fields.get("id", ""), fields["event"], json.loads(fields["data"])))
    return out


def events_url(step_id: UUID | str) -> str:
    return f"/api/steps/{step_id}/events"


async def until(cond: Callable[[], bool]) -> None:
    """Yield to the loop until `cond()` holds (bounded)."""

    async def spin() -> None:
        while not cond():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), WAIT)


def subscribe(
    env: Env, client: httpx.AsyncClient, step_id: UUID, **headers: str
) -> asyncio.Task[httpx.Response]:
    return asyncio.create_task(client.get(events_url(step_id), headers=headers))


async def done(task: Awaitable[httpx.Response]) -> httpx.Response:
    return await asyncio.wait_for(task, WAIT)


async def steer(
    client: httpx.AsyncClient, session_id: UUID, parent_id: UUID, instruction: str, **kw: Any
) -> httpx.Response:
    body = {"parent_id": str(parent_id), "wedge": "deeper", "instruction": instruction, **kw}
    return await client.post(f"/api/sessions/{session_id}/steer", json=body, headers=CSRF)


def kinds(events: list[Sse]) -> list[tuple[str, str]]:
    return [(e.event, e.id) for e in events]


# --- create -> ready -> Overview ------------------------------------------------------


async def test_ready_session_starts_overview_seen_live(env: Env) -> None:
    alice = await env.client("alice")
    gate = Gate()
    env.providers.add(OVERVIEW_INSTRUCTION, list_dir(), gate, answer("Overview"))
    resp = await alice.post("/api/sessions", json={"repo_id": PUBLIC}, headers=CSRF)
    assert resp.status_code == 201, resp.text
    sid = UUID(resp.json()["id"])
    prep: Preparer = env.app.state.preparer
    while prep.tasks:
        await asyncio.gather(*list(prep.tasks))
    await asyncio.wait_for(gate.reached.wait(), WAIT)

    got = (await alice.get(f"/api/sessions/{sid}")).json()
    assert got["status"] == "ready"
    [step] = got["steps"]
    assert step["parent_node_id"] is None and step["status"] == "running"
    stream = subscribe(env, alice, step["id"])
    await until(lambda: env.app.state.bus.subscriber_count(UUID(step["id"])) == 1)
    gate.release.set()
    resp = await done(stream)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-accel-buffering"] == "no"
    events = parse_sse(resp.text)
    assert [e.event for e in events] == ["status", "tool_done", "waypoint"]
    assert events[0].data == {"seq": 0, "type": "status", "status": "running"}
    assert [e.id for e in events] == ["0", "2", "4"]
    assert all(int(e.id) == e.data["seq"] for e in events[1:])
    wp = events[-1].data["node"]
    assert wp["waypoint"]["title"] == "Overview"
    assert wp["steer"]["wedge"] == OVERVIEW_WEDGE
    assert wp["steer"]["instruction"] == OVERVIEW_INSTRUCTION

    await env.runner.drain()
    got = (await alice.get(f"/api/sessions/{sid}")).json()
    assert [n["kind"] for n in got["nodes"]] == ["root", "tool", "waypoint"]
    assert got["steps"] == [{"id": step["id"], "status": "done", "parent_node_id": None}]
    assert got["nodes"][2]["id"] == wp["id"]


async def test_overview_starts_once(env: Env) -> None:
    """A second ready transition cannot happen, and a second admit is refused."""
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner, status="preparing")
    prep: Preparer = env.app.state.preparer
    await prep.prepare(session)  # the cached snapshot: ready, Overview admitted
    await prep.prepare(session)  # no longer preparing: nothing happens
    await env.runner.drain()
    async with env.sm() as db:
        steps = await repo.session_steps(db, session.id)
        await prep.start_overview(session)  # refused by the one-Overview index; logged
        assert len(await repo.session_steps(db, session.id)) == 1
    assert [(s.parent_node_id, s.status) for s in steps] == [(None, "done")]


# --- steering ---------------------------------------------------------------------------


async def test_steer_from_overview_context_excludes_siblings(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    assert overview.kind == "waypoint"

    env.providers.add("SIBLING_A please", answer("SIBLING_A"))
    resp = await steer(alice, session.id, overview.id, "  SIBLING_A please  ")
    assert resp.status_code == 201, resp.text
    a_id = UUID(resp.json()["step_id"])
    await env.runner.drain()
    a_step = await env.step(a_id)
    assert a_step.instruction == "SIBLING_A please"  # stripped
    assert a_step.parent_node_id == overview.id and a_step.status == "done"

    b = env.providers.add("then B", list_dir(), answer("Second"))
    resp = await steer(alice, session.id, overview.id, "then B", allow_command="ls -la")
    assert resp.status_code == 201
    b_id = UUID(resp.json()["step_id"])
    await env.runner.drain()
    b_step = await env.step(b_id)
    assert b_step.allow_command == "ls -la" and b_step.status == "done"
    b_nodes = await env.nodes(b_id)
    assert [n.kind for n in b_nodes] == ["tool", "waypoint"]
    assert b_nodes[0].parent_id == overview.id
    assert b_nodes[-1].waypoint is not None and b_nodes[-1].waypoint.title == "Second"
    seen = "\n".join(m.text for call in b.calls for m in call)
    assert "Mock answer" in seen  # the Overview, on B's path
    assert "SIBLING_A" not in seen  # the sibling branch never leaks in
    async with env.sm() as db:
        path = await repo.root_path(db, b_nodes[-1].id)
    assert [n.kind for n in path] == ["root", "tool", "waypoint", "tool", "waypoint"]

    # Steering from the new waypoint works too, and so does steering an error node.
    resp = await steer(alice, session.id, b_nodes[-1].id, "deeper still")
    assert resp.status_code == 201
    await env.runner.drain()


async def test_steer_validation_and_states(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    other = await env.session(owner)
    other_wp = await env.overview(other, owner)
    async with env.sm() as db:
        root = (await repo.load_nodes(db, session.id))[0]

    bad = [root.id, other_wp.id, uuid4()]  # wrong kind, another session, missing
    answers = [await steer(alice, session.id, p, "x") for p in bad]
    assert {r.status_code for r in answers} == {422}
    assert {json.dumps(r.json()) for r in answers} == {
        json.dumps({"detail": {"reason": "bad_parent"}})
    }

    for instruction in ("", "   ", "x" * 2001):
        assert (await steer(alice, session.id, overview.id, instruction)).status_code == 422

    preparing = await env.session(owner, status="preparing")
    resp = await steer(alice, preparing.id, overview.id, "x")
    assert resp.status_code == 409 and resp.json()["detail"] == {"reason": "session_not_ready"}

    resp = await alice.post(
        f"/api/sessions/{session.id}/steer",
        json={"parent_id": str(overview.id), "wedge": "deeper", "instruction": "x"},
    )
    assert resp.status_code == 403  # CSRF
    async with env.sm() as db:
        assert len(await repo.session_steps(db, session.id)) == 1  # nothing was admitted


async def test_viewer_403_stranger_404(env: Env) -> None:
    alice, bob, mallory = [await env.client(n) for n in USERS]
    owner = await env.user(alice)
    session = await env.session(owner, shared=True)
    overview = await env.overview(session, owner)
    assert overview.step_id is not None

    assert (await steer(bob, session.id, overview.id, "x")).status_code == 403
    assert (await steer(mallory, session.id, overview.id, "x")).status_code == 404
    viewer = await done(bob.get(events_url(overview.step_id)))
    assert viewer.status_code == 200
    assert [e.event for e in parse_sse(viewer.text)] == ["status", "tool_done", "waypoint"]
    stranger = await mallory.get(events_url(overview.step_id))
    missing = await mallory.get(events_url(uuid4()))
    owner_missing = await alice.get(events_url(uuid4()))
    assert stranger.status_code == missing.status_code == owner_missing.status_code == 404
    assert stranger.json() == missing.json() == owner_missing.json()
    async with env.sm() as db:
        assert len(await repo.session_steps(db, session.id)) == 1


# --- the event stream -----------------------------------------------------------------


async def test_mid_step_subscriber_gets_status_replay_then_live(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    replayed = asyncio.Event()
    real_step_nodes = repo.step_nodes

    async def step_nodes(db: AsyncSession, step_id: UUID) -> list[GraphNode]:
        nodes = await real_step_nodes(db, step_id)
        replayed.set()  # the stream has read what it replays
        return nodes

    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    monkeypatch.setattr(repo, "step_nodes", step_nodes)
    gate = Gate()
    env.providers.add("mid", list_dir(), gate, list_dir(), answer("Done"))
    step_id = UUID((await steer(alice, session.id, overview.id, "mid")).json()["step_id"])
    await asyncio.wait_for(gate.reached.wait(), WAIT)  # the first tool node is persisted

    stream = subscribe(env, alice, step_id)
    await asyncio.wait_for(replayed.wait(), WAIT)
    gate.release.set()
    events = parse_sse((await done(stream)).text)
    assert kinds(events) == [
        ("status", "0"),
        ("tool_done", "2"),  # replayed
        ("tool_started", "3"),  # live from here
        ("tool_done", "4"),
        ("waypoint", "6"),
    ]
    assert events[0].data["status"] == "running"
    assert events[2].data == {
        "seq": 3,
        "type": "tool_started",
        "name": "list_dir",
        "args": {"path": "."},
        "why": "look",
    }
    nodes = await env.nodes(step_id)
    assert [e.data["node"]["id"] for e in events if "node" in e.data] == [str(n.id) for n in nodes]


async def test_live_and_replay_overlap_sends_each_node_once(env: Env) -> None:
    """Released before the replay read: nodes both replayed and published go out once."""
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    gate = Gate()
    env.providers.add("race", list_dir(), gate, list_dir(), answer("Done"))
    step_id = UUID((await steer(alice, session.id, overview.id, "race")).json()["step_id"])
    await asyncio.wait_for(gate.reached.wait(), WAIT)
    stream = subscribe(env, alice, step_id)
    await until(lambda: env.app.state.bus.subscriber_count(step_id) == 1)
    gate.release.set()
    events = parse_sse((await done(stream)).text)
    node_ids = [int(e.id) for e in events if e.event != "status" and e.event != "tool_started"]
    assert node_ids == [2, 4, 6]  # every persisted node exactly once, in order
    assert [int(e.id) for e in events] == sorted(int(e.id) for e in events)


async def test_last_event_id_resume(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    assert overview.step_id is not None
    url = events_url(overview.step_id)

    full = parse_sse((await alice.get(url)).text)
    assert kinds(full) == [("status", "0"), ("tool_done", "2"), ("waypoint", "4")]
    resumed = parse_sse((await alice.get(url, headers={"Last-Event-ID": "2"})).text)
    # status carries the resume cursor as its id, so it never rewinds lastEventId
    assert kinds(resumed) == [("status", "2"), ("waypoint", "4")]
    assert resumed[0].data["seq"] == 0 and resumed[0].data["status"] == "done"
    for bad in ("abc", "-5", ""):
        got = parse_sse((await alice.get(url, headers={"Last-Event-ID": bad})).text)
        assert kinds(got) == kinds(full)
    beyond = parse_sse((await alice.get(url, headers={"Last-Event-ID": "99999999999"})).text)
    assert kinds(beyond) == [("status", str(10**9))]


def test_parse_last_event_id() -> None:
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id(" 7 ") == 7
    assert parse_last_event_id("-1") == 0
    assert parse_last_event_id("1e3") == 0
    assert parse_last_event_id(str(10**12)) == 10**9


async def test_revoked_access_closes_the_stream(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.app.state.sse_recheck_s = 0.05
    replays = 0
    real_step_nodes = repo.step_nodes

    async def step_nodes(db: AsyncSession, step_id: UUID) -> list[GraphNode]:
        nonlocal replays
        nodes = await real_step_nodes(db, step_id)
        replays += 1  # one stream has read what it replays
        return nodes

    alice, bob = await env.client("alice"), await env.client("bob")
    owner = await env.user(alice)
    session = await env.session(owner, shared=True)
    overview = await env.overview(session, owner)
    gate = Gate()
    env.providers.add("held", list_dir(), gate, answer("late"))
    step_id = UUID((await steer(alice, session.id, overview.id, "held")).json()["step_id"])
    await asyncio.wait_for(gate.reached.wait(), WAIT)

    monkeypatch.setattr(repo, "step_nodes", step_nodes)
    viewer = subscribe(env, bob, step_id)
    owner_stream = subscribe(env, alice, step_id)
    await until(lambda: replays == 2)  # both replays done: revoke only after them
    env.gh.revoke_access("bob", DEMO)
    resp = await done(viewer)  # ends while the step is still running
    assert resp.status_code == 200
    assert kinds(parse_sse(resp.text)) == [("status", "0"), ("tool_done", "2")]
    assert not owner_stream.done()
    assert env.app.state.bus.subscriber_count(step_id) == 1

    gate.release.set()
    owner_events = parse_sse((await done(owner_stream)).text)
    assert owner_events[-1].event == "waypoint"


# --- lifecycle --------------------------------------------------------------------------


async def test_restart_marks_open_steps_interrupted(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    async with env.sm() as db:
        kw: dict[str, Any] = {
            "session_id": session.id,
            "parent_node_id": overview.id,
            "steered_by": owner.id,
            "wedge": "deeper",
            "instruction": "left over",
            "mode": "safe",
            "provider": "mock",
            "model": "mock",
        }
        queued = await repo.create_step(db, **kw)
        running = await repo.create_step(db, **kw, status="running")

    async with env.app.router.lifespan_context(env.app):
        for step_id in (queued.id, running.id):
            assert (await env.step(step_id)).status == "interrupted"
            [node] = await env.nodes(step_id)
            assert (node.kind, node.error, node.parent_id) == ("error", "interrupted", overview.id)
        assert (await env.step(overview.step_id or uuid4())).status == "done"  # untouched
        events = parse_sse((await alice.get(events_url(running.id))).text)
        assert kinds(events) == [("status", "0"), ("error", "2")]
        assert events[0].data["status"] == "interrupted"


async def test_cancel_session_cancels_running_step_and_closes_streams(env: Env) -> None:
    """What session deletion (T29) does before purging."""
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    gate = Gate()
    env.providers.add("long", list_dir(), gate, answer("never"))
    step_id = UUID((await steer(alice, session.id, overview.id, "long")).json()["step_id"])
    await asyncio.wait_for(gate.reached.wait(), WAIT)
    stream = subscribe(env, alice, step_id)
    await until(lambda: env.app.state.bus.subscriber_count(step_id) == 1)

    await env.runner.cancel_session(session.id)
    assert env.runner.tasks(session.id) == []
    events = parse_sse((await done(stream)).text)
    assert [e.event for e in events] == ["status", "tool_done", "error"]
    assert events[-1].data["node"]["error"] == ERR_CANCELLED
    assert (await env.step(step_id)).status == "error"
    assert env.app.state.bus.channel_count() == 0
    async with env.sm() as db:
        assert await repo.purge(db, owner.id, [session.id]) == 1


async def test_shutdown_closes_every_running_step(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    gates, step_ids = [], []
    sessions = [await env.session(owner) for _ in range(2)]
    overviews = [await env.overview(s, owner) for s in sessions]  # drains: before any hold
    for i, (session, overview) in enumerate(zip(sessions, overviews, strict=True)):
        gate = Gate()
        env.providers.add(f"hold {i}", gate, answer("never"))
        resp = await steer(alice, session.id, overview.id, f"hold {i}")
        step_ids.append(UUID(resp.json()["step_id"]))
        gates.append(gate)
    for gate in gates:
        await asyncio.wait_for(gate.reached.wait(), WAIT)
    await env.runner.shutdown()
    assert env.runner.tasks() == []
    for step_id in step_ids:
        assert (await env.step(step_id)).status == "error"
        assert [(n.kind, n.error) for n in await env.nodes(step_id)] == [("error", ERR_CANCELLED)]


async def test_snapshot_failure_ends_in_one_error_node(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    broken = await env.session(owner, repo_id=PUBLIC)  # SHA 000... does not exist there
    async with env.sm() as db:
        step = await env.runner.admit(
            db, broken, owner, parent_node_id=None, wedge="overview", instruction="x"
        )
    await env.runner.drain()
    assert (await env.step(step.id)).status == "error"
    [node] = await env.nodes(step.id)
    assert node.kind == "error" and node.error == ERR_GITHUB
    assert overview.kind == "waypoint"


async def test_submit_twice_is_single_dispatch(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    async with env.sm() as db:
        step = await env.runner.admit(
            db, session, owner, parent_node_id=None, wedge="overview", instruction="x"
        )
    env.runner.submit(step, session, owner)
    assert len(env.runner.tasks(session.id)) == 1
    await env.runner.drain()
    assert [n.kind for n in await env.nodes(step.id)] == ["tool", "waypoint"]


async def test_submit_after_the_task_ended_does_not_run_again(env: Env) -> None:
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)
    overview = await env.overview(session, owner)
    assert overview.step_id is not None
    step = await env.step(overview.step_id)
    again = env.providers.add(step.instruction, answer("again"))
    env.runner.submit(step, session, owner)  # the store refuses: the step is not queued
    await env.runner.drain()
    assert again.calls == []  # never ran
    assert [n.kind for n in await env.nodes(step.id)] == ["tool", "waypoint"]
    assert (await env.step(step.id)).status == "done"


async def test_startup_starts_a_missing_overview(env: Env) -> None:
    """A ready session whose Overview was never admitted (crash) gets it at startup."""
    alice = await env.client("alice")
    owner = await env.user(alice)
    session = await env.session(owner)  # ready, no steps
    with_overview = await env.session(owner)
    await env.overview(with_overview, owner)
    preparing = await env.session(owner, status="preparing")

    async with env.app.router.lifespan_context(env.app):
        await env.runner.drain()
        async with env.sm() as db:
            [step] = await repo.session_steps(db, session.id)
            assert len(await repo.session_steps(db, with_overview.id)) == 1  # not twice
            assert await repo.session_steps(db, preparing.id) == []
        assert step.parent_node_id is None and step.status == "done"
        assert (step.wedge, step.instruction) == (OVERVIEW_WEDGE, OVERVIEW_INSTRUCTION)
        assert [n.kind for n in await env.nodes(step.id)][-1] == "waypoint"
        async with env.sm() as db:  # a second admit is refused quietly
            await env.runner.start_overview(db, session, owner)
            assert len(await repo.session_steps(db, session.id)) == 1

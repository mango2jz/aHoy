"""T22: the route sweep (SPEC -> Authorization). Every route of the real app is enumerated:

- every `/api/*` route except `/api/health` answers 401 when signed out (CSRF headers sent,
  so the 401 comes from auth, not from the CSRF check);
- every state-changing route except `/webhooks/github` answers 403 without the CSRF headers;
- every `/api/*` route with a path parameter (any name, any converter, any method incl.
  HEAD) must have a SWEEP_TABLE entry, and each entry is exercised for four callers:
    owner              alice, on her own shared session
    shared_viewer      bob (can read the repo), on alice's shared session
    not_shared_viewer  bob, on alice's unshared session
    stranger           mallory (cannot read the private repo), on alice's unshared session

Path parameters are filled by name (FILL_RULES): `id`/`session_id` -> the session,
`step_id` -> a closed (interrupted) step of it, `node_id` -> its root node, `parent_id`
(JSON bodies) -> that step's error node, which can be steered. A parameter without a rule fails with
"add a fill rule". A route added without an entry fails
`test_every_id_route_has_a_sweep_entry`. Test-only apps
with dummy routes prove that mechanism and that entries are really exercised.
"""

import re
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy import authz
from ahoy.app import create_app
from ahoy.auth import crypto, sessions
from ahoy.auth.deps import CurrentUser
from ahoy.auth.sessions import DB, AuthState
from ahoy.authz import Action
from ahoy.config import Settings
from ahoy.github.repo_cache import RepoCache
from ahoy.harness.schemas import Session
from ahoy.store import auth as auth_store
from ahoy.store import repo
from ahoy.store.models import GraphNodeRow, StepRow
from conftest import MakeSession, SameSession, SignUp

ORIGIN = "http://127.0.0.1:5173"
CSRF = {"Origin": ORIGIN, "X-Ahoy-CSRF": "1"}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_API = frozenset({"/api/health"})
CSRF_EXEMPT = frozenset({"/webhooks/github"})
PATH_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")  # {name} or {name:converter}
# Path parameter name -> which object of the session under test fills it.
FILL_RULES = {
    "id": "session",
    "session_id": "session",
    "step_id": "step",
    "node_id": "node",
    "parent_id": "parent",  # a steerable (error) node: the closed step's final node
}
ANY_ID = "00000000-0000-4000-8000-000000000000"
SWEEP_FILE = "README.md"  # in the snapshot seeded for every sweep session (seed_snapshot)


@dataclass(frozen=True)
class Expect:
    owner: int
    shared_viewer: int
    stranger: int
    not_shared_viewer: int
    query: Mapping[str, str] | None = None  # query parameters the route needs
    # JSON body the route needs; a "{name}" value is filled like a path parameter.
    json: Mapping[str, str] | None = None


# (path, METHOD) -> expected status per caller. Every id route of the app must be here.
# T23b adds /api/sessions/{id}; later tasks add theirs with the route.
SWEEP_TABLE: dict[tuple[str, str], Expect] = {
    ("/api/sessions/{id}", "GET"): Expect(
        owner=200, shared_viewer=200, stranger=404, not_shared_viewer=404
    ),
    ("/api/sessions/{id}/files", "GET"): Expect(
        owner=200,
        shared_viewer=200,
        stranger=404,
        not_shared_viewer=404,
        query={"path": SWEEP_FILE},
    ),
    ("/api/sessions/{id}/steer", "POST"): Expect(
        owner=201,
        shared_viewer=403,
        stranger=404,
        not_shared_viewer=404,
        json={"parent_id": "{parent_id}", "wedge": "deeper", "instruction": "sweep"},
    ),
    ("/api/steps/{step_id}/events", "GET"): Expect(
        owner=200, shared_viewer=200, stranger=404, not_shared_viewer=404
    ),
}


# --- route enumeration ----------------------------------------------------------------


@dataclass(frozen=True)
class RouteInfo:
    path: str  # as declared, converters included
    path_format: str  # as OpenAPI shows it
    methods: frozenset[str]
    in_schema: bool

    @property
    def params(self) -> list[str]:
        return PATH_PARAM.findall(self.path)


def iter_routes(routes: Any, prefix: str = "") -> Iterator[RouteInfo]:
    """Every route, looking inside included routers (FastAPI 0.165 wraps them)."""
    for route in routes:
        sub = getattr(route, "original_router", None)
        if sub is not None:
            context = getattr(route, "include_context", None)
            yield from iter_routes(sub.routes, prefix + getattr(context, "prefix", ""))
        elif isinstance(getattr(route, "path", None), str):
            yield RouteInfo(
                prefix + route.path,
                prefix + getattr(route, "path_format", route.path),
                frozenset(getattr(route, "methods", None) or ()),
                bool(getattr(route, "include_in_schema", True)),
            )


def app_routes(app: FastAPI) -> list[RouteInfo]:
    """The app's routes, cross-checked against its OpenAPI paths and methods."""
    found = list(iter_routes(app.routes))
    schema: dict[str, set[str]] = {}
    for info in found:
        if info.in_schema:
            schema.setdefault(info.path_format, set()).update(m.lower() for m in info.methods)
    openapi = {path: set(ops) for path, ops in app.openapi()["paths"].items()}
    assert schema == openapi, "route walk and OpenAPI disagree"
    return found


def missing_sweep_entries(
    app: FastAPI, table: Mapping[tuple[str, str], Expect]
) -> list[tuple[str, str]]:
    return sorted(
        (info.path, method)
        for info in app_routes(app)
        if info.path.startswith("/api/") and info.params
        for method in info.methods
        if (info.path, method) not in table
    )


def check_sweep_table(app: FastAPI, table: Mapping[tuple[str, str], Expect]) -> None:
    unfillable = sorted(
        f"{{{name}}} in {info.path}"
        for info in app_routes(app)
        if info.path.startswith("/api/")
        for name in info.params
        if name not in FILL_RULES
    )
    if unfillable:
        pytest.fail(
            f"no fill rule for {', '.join(unfillable)}: add a fill rule to FILL_RULES "
            "(and to _fill) so the sweep can build a real URL",
            pytrace=False,
        )
    missing = missing_sweep_entries(app, table)
    if missing:
        listed = ", ".join(f"{method} {path}" for path, method in missing)
        pytest.fail(
            f"/api routes with a path parameter but no SWEEP_TABLE entry: {listed}. "
            "Add an Expect(owner, shared_viewer, stranger, not_shared_viewer) for each.",
            pytrace=False,
        )
    present = {(i.path, m) for i in app_routes(app) for m in i.methods}
    stale = sorted(set(table) - present)
    assert not stale, f"SWEEP_TABLE entries for routes that do not exist: {stale}"


# --- app + callers --------------------------------------------------------------------


def _settings(cache_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        ahoy_env="test",
        database_url=None,
        app_origin=ORIGIN,
        token_encryption_key=crypto.generate_key(),
        repo_cache_dir=str(cache_dir),
    )


def make_app(db: AsyncSession, cache_dir: Path) -> FastAPI:
    """The real app (mock provider + mock GitHub), bound to the test's DB session."""
    app = create_app(_settings(cache_dir), "mock", "mock")

    async def override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[sessions.get_db] = override
    auth: AuthState = app.state.auth
    auth.sessionmaker = SameSession(db).maker()
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _signed_in(stack: AsyncExitStack, app: FastAPI, name: str) -> httpx.AsyncClient:
    client = await stack.enter_async_context(_client(app))
    resp = await client.get("/auth/mock/login", params={"user": name})
    assert resp.status_code == 303, resp.text
    return client


async def _fill(
    db: AsyncSession, path: str, session: Session, made: dict[UUID, dict[str, str]]
) -> str:
    """`path` with every parameter replaced by the matching id of `session` (FILL_RULES).
    `made` remembers the step and node per session across calls."""
    ids = made.setdefault(session.id, {"session": str(session.id)})
    for name in PATH_PARAM.findall(path):
        kind = FILL_RULES.get(name)
        if kind is None:
            pytest.fail(f"no fill rule for {{{name}}} in {path}: add a fill rule", pytrace=False)
        if kind == "node" and kind not in ids:
            root = (await repo.load_nodes(db, session.id))[0]
            ids["node"] = str(root.id)
        if kind in ("step", "parent") and "step" not in ids:
            step = await repo.create_step(
                db,
                session_id=session.id,
                parent_node_id=None,
                steered_by=session.owner_id,
                wedge="overview",
                instruction="sweep",
                mode="safe",
                provider="mock",
                model="mock",
            )
            # Closed, so its event stream ends after the replay.
            _step, error = await repo.mark_interrupted(db, step.id)
            ids["step"], ids["parent"] = str(step.id), str(error.id)
    return PATH_PARAM.sub(lambda m: ids[FILL_RULES[m.group(1)]], path)


async def seed_snapshot(app: FastAPI, session: Session) -> None:
    """Cache a tiny snapshot under the key every `make_session` session uses (same repo
    and SHA), so file routes have a file to serve."""

    async def fetch(dest: Path) -> None:
        (dest / SWEEP_FILE).write_text("sweep\n")

    cache: RepoCache = app.state.repo_cache
    async with cache.lease(session.repo_id, session.commit_sha, fetch):
        pass


async def _fill_json(
    db: AsyncSession, body: Mapping[str, str], session: Session, made: dict[UUID, dict[str, str]]
) -> dict[str, str]:
    return {k: await _fill(db, v, session, made) for k, v in body.items()}


async def _send(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
    if method not in SAFE_METHODS:
        kw.setdefault("json", {})
    return await client.request(method, url, **kw)


async def run_sweep(
    app: FastAPI,
    db: AsyncSession,
    make_session: MakeSession,
    table: Mapping[tuple[str, str], Expect],
) -> int:
    """Exercise every entry for all four callers; returns the number of requests made."""
    sent = 0
    made: dict[UUID, dict[str, str]] = {}
    async with AsyncExitStack() as stack:
        alice, bob, mallory = [
            await _signed_in(stack, app, name) for name in ("alice", "bob", "mallory")
        ]
        me = (await alice.get("/api/me")).json()
        owner = await auth_store.get_user(db, UUID(me["id"]))
        assert owner is not None
        for (path, method), expect in table.items():
            # Fresh sessions per entry: an entry may change or delete its session.
            shared = await make_session(owner, shared=True)
            unshared = await make_session(owner)
            # The owner goes last, so an owner-only effect cannot hide the others' answers.
            cases = (
                ("shared_viewer", bob, shared, expect.shared_viewer),
                ("not_shared_viewer", bob, unshared, expect.not_shared_viewer),
                ("stranger", mallory, unshared, expect.stranger),
                ("owner", alice, shared, expect.owner),
            )
            await seed_snapshot(app, shared)
            for role, client, session, want in cases:
                url = await _fill(db, path, session, made)
                extra: dict[str, Any] = {}
                if expect.json is not None:
                    extra["json"] = await _fill_json(db, expect.json, session, made)
                resp = await _send(client, method, url, headers=CSRF, params=expect.query, **extra)
                # A started step must not share the test's DB session with the next call.
                runner = getattr(app.state, "runner", None)
                if runner is not None:
                    await runner.drain()
                sent += 1
                assert resp.status_code == want, (
                    f"{method} {path} as {role}: got {resp.status_code}, want {want}"
                )
    return sent


@pytest.fixture
def app(db: AsyncSession, tmp_path: Path) -> FastAPI:
    return make_app(db, tmp_path / "cache")


# --- the real app ---------------------------------------------------------------------


def test_route_walk_matches_openapi(app: FastAPI) -> None:
    paths = {info.path for info in app_routes(app)}
    assert {"/api/health", "/api/me", "/auth/logout"} <= paths


def test_every_id_route_has_a_sweep_entry(app: FastAPI) -> None:
    check_sweep_table(app, SWEEP_TABLE)


@pytest.mark.db
async def test_api_routes_401_signed_out(app: FastAPI) -> None:
    checked = []
    async with _client(app) as client:
        for info in app_routes(app):
            if not info.path.startswith("/api/") or info.path in PUBLIC_API:
                continue
            for method in info.methods:
                url = PATH_PARAM.sub(ANY_ID, info.path)  # any id: auth comes first
                resp = await _send(client, method, url, headers=CSRF)
                assert resp.status_code == 401, f"{method} {info.path}: {resp.status_code}"
                checked.append((method, info.path))
    assert ("GET", "/api/me") in checked


@pytest.mark.db
async def test_state_changing_routes_need_csrf(app: FastAPI) -> None:
    checked = []
    async with _client(app) as client:
        for info in app_routes(app):
            if info.path in CSRF_EXEMPT:
                continue
            for method in info.methods - SAFE_METHODS:
                for headers in ({}, {"Origin": ORIGIN}, {"X-Ahoy-CSRF": "1"}):
                    resp = await _send(client, method, info.path, headers=headers)
                    assert resp.status_code == 403, f"{method} {info.path} with {headers}"
                checked.append((method, info.path))
    assert ("POST", "/auth/logout") in checked


@pytest.mark.db
async def test_sweep_table_entries_hold(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    sent = await run_sweep(app, db, make_session, SWEEP_TABLE)
    assert sent == 4 * len(SWEEP_TABLE)


# --- the mechanism, proved on test-only routes ----------------------------------------


def _add_dummy_routes(app: FastAPI) -> None:
    async def run(request: Request, user: CurrentUser, db: DB, sid: UUID, action: Action) -> None:
        await authz.load_session(request, db, user, sid, action)

    @app.get("/api/_sweep/sessions/{session_id}")
    async def view(session_id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        await run(request, user, db, session_id, Action.VIEW)
        return Response(status_code=200)

    @app.post("/api/_sweep/sessions/{session_id}/steer")
    async def steer(session_id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        await run(request, user, db, session_id, Action.STEER)
        return Response(status_code=200)

    @app.delete("/api/_sweep/sessions/{id}")
    async def delete(id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        await run(request, user, db, id, Action.DELETE)
        return Response(status_code=204)

    @app.get("/api/_sweep/conv/{session_id:uuid}")
    async def conv(session_id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        await run(request, user, db, session_id, Action.VIEW)
        return Response(status_code=200)

    @app.api_route("/api/_sweep/head/{session_id}", methods=["HEAD"])
    async def head(session_id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        await run(request, user, db, session_id, Action.VIEW)
        return Response(status_code=200)

    @app.get("/api/_sweep/nodes/{node_id}")
    async def node(node_id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        stmt = select(GraphNodeRow.session_id).where(GraphNodeRow.id == node_id)
        sid = (await db.execute(stmt)).scalar_one()
        await run(request, user, db, sid, Action.VIEW)
        return Response(status_code=200)

    @app.get("/api/_sweep/steps/{step_id}")
    async def step(step_id: UUID, request: Request, user: CurrentUser, db: DB) -> Response:
        sid = (
            await db.execute(select(StepRow.session_id).where(StepRow.id == step_id))
        ).scalar_one()
        await run(request, user, db, sid, Action.SUBSCRIBE)
        return Response(status_code=200)


DUMMY_TABLE = {
    ("/api/_sweep/sessions/{session_id}", "GET"): Expect(200, 200, 404, 404),
    ("/api/_sweep/sessions/{session_id}/steer", "POST"): Expect(200, 403, 404, 404),
    ("/api/_sweep/sessions/{id}", "DELETE"): Expect(204, 403, 404, 404),
    ("/api/_sweep/conv/{session_id:uuid}", "GET"): Expect(200, 200, 404, 404),
    ("/api/_sweep/head/{session_id}", "HEAD"): Expect(200, 200, 404, 404),
    ("/api/_sweep/nodes/{node_id}", "GET"): Expect(200, 200, 404, 404),
    ("/api/_sweep/steps/{step_id}", "GET"): Expect(200, 200, 404, 404),
}


def test_missing_entry_fails(app: FastAPI) -> None:
    _add_dummy_routes(app)
    assert missing_sweep_entries(app, SWEEP_TABLE) == sorted(DUMMY_TABLE)
    with pytest.raises(pytest.fail.Exception, match=r"GET /api/_sweep/sessions/\{session_id\}"):
        check_sweep_table(app, SWEEP_TABLE)
    check_sweep_table(app, {**SWEEP_TABLE, **DUMMY_TABLE})


def test_converter_and_head_only_routes_need_entries(app: FastAPI) -> None:
    _add_dummy_routes(app)
    missing = missing_sweep_entries(app, SWEEP_TABLE)
    assert ("/api/_sweep/conv/{session_id:uuid}", "GET") in missing
    assert ("/api/_sweep/head/{session_id}", "HEAD") in missing


def test_unknown_param_name_fails_with_fill_hint(app: FastAPI) -> None:
    @app.get("/api/_sweep/other/{sid}")
    async def other(sid: UUID) -> Response:
        return Response(status_code=200)

    table = {("/api/_sweep/other/{sid}", "GET"): Expect(200, 200, 404, 404)}
    with pytest.raises(
        pytest.fail.Exception, match=r"\{sid\} in /api/_sweep/other.*add a fill rule"
    ):
        check_sweep_table(app, table)


@pytest.mark.db
async def test_fill_by_param_name(
    app: FastAPI, db: AsyncSession, make_session: MakeSession, sign_up: SignUp
) -> None:
    session = await make_session(await sign_up(app.state.auth, "alice"))
    made: dict[UUID, dict[str, str]] = {}
    with pytest.raises(pytest.fail.Exception, match="add a fill rule"):
        await _fill(db, "/api/x/{sid}", session, made)
    path = "/api/x/{session_id:uuid}/{step_id}/{node_id}/{id}"
    url = await _fill(db, path, session, made)
    assert url == await _fill(db, path, session, made)  # one step per session, reused
    _, _, _, sid, step_id, node_id, same = url.split("/")
    assert sid == same == str(session.id)
    root = (await repo.load_nodes(db, session.id))[0]
    assert node_id == str(root.id) and root.kind == "root"
    steps = await repo.session_steps(db, session.id)
    assert [str(s.id) for s in steps] == [step_id] and steps[0].status == "interrupted"


def test_stale_entry_fails(app: FastAPI) -> None:
    with pytest.raises(AssertionError, match="do not exist"):
        check_sweep_table(
            app, {**SWEEP_TABLE, ("/api/gone/{id}", "GET"): Expect(200, 200, 404, 404)}
        )


@pytest.mark.db
async def test_dummy_entries_are_exercised(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    _add_dummy_routes(app)
    assert await run_sweep(app, db, make_session, DUMMY_TABLE) == 4 * len(DUMMY_TABLE)


@pytest.mark.db
async def test_wrong_expectation_is_caught(
    app: FastAPI, db: AsyncSession, make_session: MakeSession
) -> None:
    _add_dummy_routes(app)
    lying = {("/api/_sweep/sessions/{session_id}", "GET"): Expect(200, 200, 200, 404)}
    with pytest.raises(AssertionError, match="as stranger: got 404, want 200"):
        await run_sweep(app, db, make_session, lying)

"""`GET /api/steps/{step_id}/events`: a step's events as SSE (SPEC -> API).

Authorized through the step's session (`Action.SUBSCRIBE`); a missing step and an
unseeable one are the same 404. The stream is `EventBus.replay_then_live`: a `status`
event, the persisted node events after `Last-Event-ID`, then live events, ending after the
final waypoint or error event.

Each SSE message is `id: <seq>`, `event: <type>`, `data: <the event as JSON>`. `status` and
`queued` events have seq 0 in their JSON; their SSE `id` is the stream's resume cursor (the
highest seq sent so far, or `Last-Event-ID`) instead, so they never rewind the browser's
`lastEventId` and a reconnect resumes where the client really is.

While the stream is open, access is re-checked with `fresh=True` every
`app.state.sse_recheck_s` seconds (60); losing access (404/403/401) ends the stream.
GitHub being unavailable (503) does not: the next re-check tries again.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Final
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import RootModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette import EventSourceResponse, ServerSentEvent

from ahoy.auth.deps import CurrentUser
from ahoy.auth.sessions import DB, get_auth
from ahoy.authz import Action, load_session
from ahoy.harness.events import AnyStepEvent, EventBus
from ahoy.harness.schemas import GraphNode, StepEvent, StepStatus, User
from ahoy.store import repo

log = logging.getLogger("ahoy.events")

RECHECK_S: Final = 60.0
MAX_LAST_EVENT_ID: Final = 10**9

router = APIRouter(prefix="/api")


class StepEventMessage(RootModel[StepEvent]):
    """The JSON in one SSE `data:` line (documented in OpenAPI for generated types)."""


def parse_last_event_id(raw: str | None) -> int:
    """`Last-Event-ID` as an int in [0, 10**9]; anything unparsable is 0."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return 0
    return min(max(value, 0), MAX_LAST_EVENT_ID)


async def guarded(
    events: AsyncIterator[AnyStepEvent],
    still_allowed: Callable[[], Awaitable[bool]],
    interval: float,
) -> AsyncGenerator[AnyStepEvent]:
    """`events`, ended as soon as `still_allowed()` (asked every `interval` s) is False."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval
    pending: asyncio.Future[AnyStepEvent] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=max(0.0, deadline - loop.time()))
            if done:
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield event
                continue
            if not await still_allowed():
                return
            deadline = loop.time() + interval
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


def _sse(event: AnyStepEvent, cursor: int) -> ServerSentEvent:
    sse_id = event.seq if event.seq else cursor
    return ServerSentEvent(data=event.model_dump_json(), event=event.type, id=str(sse_id))


@router.get(
    "/steps/{step_id}/events",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": "text/event-stream; each `data:` line is one StepEventMessage",
            "model": StepEventMessage,
        }
    },
)
async def step_events(
    step_id: UUID, request: Request, user: CurrentUser, db: DB
) -> EventSourceResponse:
    step = await repo.get_step(db, step_id)
    if step is None:
        raise HTTPException(404, "not found")  # the same answer as an unseeable session
    session = await load_session(request, db, user, step.session_id, Action.SUBSCRIBE)

    auth = get_auth(request)
    maker = auth.sessionmaker
    if maker is None:
        raise HTTPException(503, "database not configured")
    bus: EventBus = request.app.state.bus
    interval: float = getattr(request.app.state, "sse_recheck_s", RECHECK_S)
    last_seq = parse_last_event_id(request.headers.get("last-event-id"))

    stream = _stream(maker, bus, user, step.id, session.id, last_seq, interval, request)
    return EventSourceResponse(stream, headers={"Cache-Control": "no-store"})


async def _stream(
    maker: async_sessionmaker[AsyncSession],
    bus: EventBus,
    user: User,
    step_id: UUID,
    session_id: UUID,
    last_seq: int,
    interval: float,
    request: Request,
) -> AsyncIterator[ServerSentEvent]:
    # The stream outlives the request's DB session: every read uses its own.
    async def load_status() -> StepStatus:
        async with maker() as db:
            step = await repo.get_step(db, step_id)
        if step is None:  # purged meanwhile: nothing more will come
            return "interrupted"
        return step.status

    async def load_nodes() -> list[GraphNode]:
        async with maker() as db:
            return await repo.step_nodes(db, step_id)

    async def still_allowed() -> bool:
        try:
            async with maker() as db:
                await load_session(
                    get_auth(request), db, user, session_id, Action.SUBSCRIBE, fresh=True
                )
        except HTTPException as exc:
            if exc.status_code == 503:
                return True  # GitHub is down: not a revocation, ask again next time
            log.info("step %s stream closed: access re-check %d", step_id, exc.status_code)
            return False
        return True

    cursor = last_seq
    events = bus.replay_then_live(
        step_id, last_seq, session_id=session_id, load_status=load_status, load_nodes=load_nodes
    )
    async with (
        contextlib.aclosing(events),
        contextlib.aclosing(guarded(events, still_allowed, interval)) as checked,
    ):
        async for event in checked:
            yield _sse(event, cursor)
            cursor = max(cursor, event.seq)

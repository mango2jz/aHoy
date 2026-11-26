"""Per-step event channels (SPEC -> API, `GET /api/steps/{step_id}/events`).

In-process and asyncio-only: one `EventBus` per app (single instance assumed).

Sequence numbers. The k-th persisted node of a step (1-based, ordered by created_at then
id, as the store returns them) has seq 2k; the `tool_started` event that precedes it has
seq 2k - 1. `queued` and `status` events have seq 0. A node event therefore has the same
seq whether it is published live by the step loop or rebuilt from the DB on replay, and a
client can resume from any `Last-Event-ID`.

Channels exist only while someone listens: subscribing creates the step's channel and the
last subscriber leaving removes it, so a step nobody watches costs nothing and finished
steps leave nothing behind. `publish` never blocks: each subscriber has a bounded backlog,
and a subscriber that falls behind is dropped (its stream ends; the client reconnects with
`Last-Event-ID` and replays from the DB).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from ahoy.harness.schemas import (
    BlockedEvent,
    ErrorEvent,
    GraphNode,
    QueuedEvent,
    StatusEvent,
    StepStatus,
    ToolDoneEvent,
    ToolStartedEvent,
    WaypointEvent,
)

AnyStepEvent = (
    StatusEvent
    | QueuedEvent
    | ToolStartedEvent
    | ToolDoneEvent
    | BlockedEvent
    | WaypointEvent
    | ErrorEvent
)
NodeEvent = ToolDoneEvent | BlockedEvent | WaypointEvent | ErrorEvent

DEFAULT_BACKLOG = 256  # events a subscriber may fall behind before it is dropped
OPEN_STATUSES: Final = frozenset({"queued", "running"})
FINAL_EVENT_TYPES: Final = frozenset({"waypoint", "error"})


def node_seq(k: int) -> int:
    """Seq of the k-th persisted node of a step (1-based)."""
    if k < 1:
        raise ValueError("k is 1-based")
    return 2 * k


def tool_started_seq(k: int) -> int:
    """Seq of the `tool_started` event that precedes the k-th persisted node."""
    return node_seq(k) - 1


def node_event(node: GraphNode, seq: int) -> NodeEvent:
    """The replayable event for a persisted step node (the same for live and replay)."""
    if node.kind == "tool":
        return ToolDoneEvent(seq=seq, node=node)
    if node.kind == "blocked":
        return BlockedEvent(seq=seq, node=node)
    if node.kind == "waypoint":
        return WaypointEvent(seq=seq, node=node)
    if node.kind == "error":
        return ErrorEvent(seq=seq, node=node)
    raise ValueError(f"no event for a {node.kind} node")


class _Close:
    """Queue sentinel: the stream ends."""


_CLOSE: Final = _Close()


@dataclass(eq=False)
class _Subscriber:
    backlog: int
    queue: asyncio.Queue[AnyStepEvent | _Close] = field(default_factory=asyncio.Queue)
    closed: bool = False

    def offer(self, event: AnyStepEvent) -> bool:
        """Queue `event`; False (and closed) if the subscriber is too far behind."""
        if self.closed:
            return False
        if self.queue.qsize() >= self.backlog:
            self.close()
            return False
        self.queue.put_nowait(event)
        return True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(_CLOSE)  # unbounded queue: never raises


@dataclass(eq=False)
class _Channel:
    session_id: UUID
    subscribers: set[_Subscriber] = field(default_factory=set)


class EventBus:
    """Per-step fan-out of step events to live subscribers."""

    def __init__(self, *, backlog: int = DEFAULT_BACKLOG) -> None:
        if backlog < 1:
            raise ValueError("backlog must be >= 1")
        self._backlog = backlog
        self._channels: dict[UUID, _Channel] = {}

    # --- introspection (tests, metrics) ---------------------------------------------

    def channel_count(self) -> int:
        return len(self._channels)

    def subscriber_count(self, step_id: UUID) -> int:
        channel = self._channels.get(step_id)
        return 0 if channel is None else len(channel.subscribers)

    # --- publishing -------------------------------------------------------------------

    def publish(self, step_id: UUID, event: AnyStepEvent) -> None:
        """Hand `event` to every live subscriber of the step. Never blocks or raises.

        A subscriber whose backlog is full is dropped. When no subscriber remains, the
        channel is removed.
        """
        channel = self._channels.get(step_id)
        if channel is None:
            return
        for sub in list(channel.subscribers):
            if not sub.offer(event):
                channel.subscribers.discard(sub)
        if not channel.subscribers:
            self._channels.pop(step_id, None)

    def close_session_streams(self, session_id: UUID) -> int:
        """End every stream of every step of `session_id` now. Returns how many ended."""
        ended = 0
        for step_id, channel in list(self._channels.items()):
            if channel.session_id != session_id:
                continue
            for sub in channel.subscribers:
                sub.close()
                ended += 1
            channel.subscribers.clear()
            del self._channels[step_id]
        return ended

    # --- subscribing ------------------------------------------------------------------

    def _subscribe(self, step_id: UUID, session_id: UUID) -> _Subscriber:
        channel = self._channels.get(step_id)
        if channel is None:
            channel = self._channels[step_id] = _Channel(session_id=session_id)
        elif channel.session_id != session_id:
            raise ValueError("step belongs to another session")
        sub = _Subscriber(backlog=self._backlog)
        channel.subscribers.add(sub)
        return sub

    def _unsubscribe(self, step_id: UUID, sub: _Subscriber) -> None:
        sub.closed = True
        channel = self._channels.get(step_id)
        if channel is None:
            return
        channel.subscribers.discard(sub)
        if not channel.subscribers:
            del self._channels[step_id]

    async def replay_then_live(
        self,
        step_id: UUID,
        last_seq: int,
        *,
        session_id: UUID,
        load_status: Callable[[], Awaitable[StepStatus]],
        load_nodes: Callable[[], Awaitable[list[GraphNode]]],
    ) -> AsyncGenerator[AnyStepEvent]:
        """A step's event stream for one client, resuming after `last_seq` (0 = from start).

        1. Subscribes first, so nothing published from now on is missed.
        2. Yields `StatusEvent(seq=0)` with the step's status (`load_status`, read before
           the nodes: a closed status guarantees the nodes include the final one, because
           the store closes a step and inserts its final node in one transaction).
        3. Replays the persisted node events (`load_nodes`: the step's nodes in store
           order) with seq > `last_seq`.
        4. Streams live events with seq > the highest seq already sent (so a node that was
           both replayed and published is sent once); seq-0 events (`queued`, `status`)
           always pass, and so does the live final (waypoint/error) event, so a
           `last_seq` beyond the end (stale or bogus Last-Event-ID; clamped to >= 0)
           still ends the stream.

        Ends after a waypoint or error event, right after replay if the step was already
        closed, or when the stream is dropped or closed (`close_session_streams`).
        """
        last_seq = max(0, last_seq)
        sub = self._subscribe(step_id, session_id)
        try:
            status = await load_status()
            yield StatusEvent(seq=0, status=status)
            nodes = await load_nodes()
            sent = last_seq
            for k, node in enumerate(nodes, start=1):
                seq = node_seq(k)
                if seq <= last_seq:
                    continue
                event = node_event(node, seq)
                yield event
                sent = seq
                if event.type in FINAL_EVENT_TYPES:
                    return
            if status not in OPEN_STATUSES:
                return
            while True:
                item = await sub.queue.get()
                if isinstance(item, _Close):
                    return
                final = item.type in FINAL_EVENT_TYPES
                if not final and item.seq != 0 and item.seq <= sent:
                    continue
                yield item
                sent = max(sent, item.seq)
                if final:
                    return
        finally:
            self._unsubscribe(step_id, sub)

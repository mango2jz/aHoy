"""Store API (SPEC -> Data model). Every function takes an AsyncSession and returns Pydantic
models from harness/schemas.py, never ORM rows.

Writes commit before they return, so a node is persisted before anyone broadcasts it. On any
error the session is rolled back and the error re-raised. Reads commit right after the read,
so a long-lived session never sits idle in a transaction holding a snapshot. Graph nodes are
only ever inserted (ahoy_app has no UPDATE or DELETE on graph_nodes); whole sessions go
through purge().

Lock order. Node writers (append_node, close_step, mark_interrupted) lock the session row
FOR KEY SHARE first, then the step row. purge_sessions() locks the session rows FOR UPDATE
before deleting, so it simply waits for in-flight writers (and they find the session gone
afterwards) instead of deadlocking against them.

Branch integrity. A step's nodes hang off the step's parent node (the session root for the
Overview step, whose parent_node_id is NULL) or off an earlier node of the same step. Nothing
can attach to a sibling branch, which keeps each node's root path its only context.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Row, column, func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.harness.schemas import (
    CommandMode,
    GraphNode,
    Session,
    SessionStatus,
    Step,
    StepStatus,
)
from ahoy.store.models import GraphNodeRow, SessionRow, StepRow

OPEN_STEP_STATUSES: tuple[StepStatus, ...] = ("queued", "running")
CLOSED_STEP_STATUSES: tuple[StepStatus, ...] = ("done", "error", "interrupted")
FINAL_NODE_KINDS = ("waypoint", "error")
INTERRUPTED_ERROR = "interrupted"
DEADLOCK_DETECTED = "40P01"

_JSONB_FIELDS = ("steer", "tool", "blocked", "waypoint")
_NODE_ORDER = (GraphNodeRow.created_at, GraphNodeRow.id)


class StoreError(Exception):
    """A store call was refused (missing row, closed step, inconsistent arguments)."""


# Timestamps use clock_timestamp(), not now(): now() is the transaction start, so rows made in
# one transaction would tie and order by their random ids.


class NotFoundError(StoreError):
    pass


class StepStateError(StoreError):
    """The step is not in the state the call needs."""


class StepClosedError(StepStateError):
    pass


async def _read[T](db: AsyncSession, work: Callable[[], Awaitable[T]]) -> T:
    """Run a read and end its transaction, so the connection is not left idle in one.

    Commit, not rollback: a caller's own pending work in the same session is kept.
    """
    try:
        result = await work()
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return result


async def _write[T](db: AsyncSession, work: Callable[[], Awaitable[T]]) -> T:
    """Run `work` and commit; roll back and re-raise on any error."""
    try:
        result = await work()
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return result


def _session(row: Row[Any]) -> Session:
    return Session.model_validate(dict(row._mapping))


def _step(row: Row[Any]) -> Step:
    return Step.model_validate(dict(row._mapping))


def _node(row: Row[Any]) -> GraphNode:
    return GraphNode.model_validate(dict(row._mapping))


async def _all[T](db: AsyncSession, stmt: Any, convert: Callable[[Row[Any]], T]) -> list[T]:
    return [convert(row) for row in (await db.execute(stmt)).all()]


def _node_values(node: GraphNode) -> dict[str, Any]:
    values = node.model_dump()
    for name in _JSONB_FIELDS:
        payload = getattr(node, name)
        values[name] = None if payload is None else payload.model_dump(mode="json")
    return values


async def _insert_node(db: AsyncSession, node: GraphNode) -> GraphNode:
    """Insert `node` and return it as stored. created_at always comes from the DB clock."""
    # The parent FK is satisfied by the row itself, so only this check stops a self-loop
    # (root_path would recurse forever on one).
    if node.parent_id == node.id:
        raise StoreError("a node cannot be its own parent")
    values = _node_values(node) | {"created_at": func.clock_timestamp()}
    stmt = insert(GraphNodeRow).values(values).returning(*GraphNodeRow.__table__.c)
    return _node((await db.execute(stmt)).one())


# --- sessions ------------------------------------------------------------------------


async def create_session(
    db: AsyncSession,
    *,
    owner_id: UUID,
    installation_id: int,
    repo_id: int,
    repo_full_name: str,
    ref: str,
    commit_sha: str,
    private: bool,
    model: str,
    status: SessionStatus = "preparing",
) -> tuple[Session, GraphNode]:
    """Insert the session and its root node (kind "root", no step) in one transaction.

    `model` is recorded on the root node.
    """
    stmt = (
        insert(SessionRow)
        .values(
            owner_id=owner_id,
            installation_id=installation_id,
            repo_id=repo_id,
            repo_full_name=repo_full_name,
            ref=ref,
            commit_sha=commit_sha,
            private=private,
            status=status,
            created_at=func.clock_timestamp(),
        )
        .returning(*SessionRow.__table__.c)
    )

    async def work() -> tuple[Session, GraphNode]:
        session = _session((await db.execute(stmt)).one())
        root = GraphNode(
            id=uuid4(),
            session_id=session.id,
            step_id=None,
            parent_id=None,
            kind="root",
            model=model,
            created_at=session.created_at,  # replaced by the DB clock on insert
        )
        return session, await _insert_node(db, root)

    return await _write(db, work)


async def set_session_status(
    db: AsyncSession, session_id: UUID, status: SessionStatus, reason: str | None = None
) -> Session:
    stmt = (
        update(SessionRow)
        .where(SessionRow.id == session_id)
        .values(status=status, status_reason=reason)
        .returning(*SessionRow.__table__.c)
    )

    async def work() -> Session:
        row = (await db.execute(stmt)).one_or_none()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return _session(row)

    return await _write(db, work)


# --- steps ---------------------------------------------------------------------------


async def create_step(
    db: AsyncSession,
    *,
    session_id: UUID,
    parent_node_id: UUID | None,
    steered_by: UUID,
    wedge: str,
    instruction: str,
    mode: CommandMode,
    provider: str,
    model: str,
    allow_command: str | None = None,
    status: StepStatus = "queued",
) -> Step:
    if status not in OPEN_STEP_STATUSES:
        raise StoreError(f"a new step must be open, not {status!r}")
    stmt = (
        insert(StepRow)
        .values(
            session_id=session_id,
            parent_node_id=parent_node_id,
            steered_by=steered_by,
            wedge=wedge,
            instruction=instruction,
            allow_command=allow_command,
            mode=mode,
            provider=provider,
            model=model,
            status=status,
            created_at=func.clock_timestamp(),
            started_at=func.clock_timestamp() if status == "running" else None,
        )
        .returning(*StepRow.__table__.c)
    )

    async def work() -> Step:
        # Same first lock as the node writers, so a purged session is NotFoundError and the
        # order does not hang on which FK trigger fires first.
        await _lock_session(db, session_id)
        try:
            return _step((await db.execute(stmt)).one())
        except IntegrityError as exc:
            if "uq_steps_one_overview" in str(exc.orig):
                raise StoreError(f"session {session_id} already has an Overview step") from exc
            raise StoreError(
                "the step's parent node, session or user does not exist in that session"
            ) from exc

    return await _write(db, work)


async def start_step(db: AsyncSession, step_id: UUID) -> Step:
    """queued -> running, setting started_at. Refuses a step in any other state."""
    stmt = (
        update(StepRow)
        .where(StepRow.id == step_id, StepRow.status == "queued")
        .values(status="running", started_at=func.clock_timestamp())
        .returning(*StepRow.__table__.c)
    )

    async def work() -> Step:
        row = (await db.execute(stmt)).one_or_none()
        if row is not None:
            return _step(row)
        status = (
            await db.execute(select(StepRow.status).where(StepRow.id == step_id))
        ).scalar_one_or_none()
        if status is None:
            raise NotFoundError(f"step {step_id} not found")
        raise StepStateError(f"step {step_id} is {status}, not queued")

    return await _write(db, work)


async def _lock_session(db: AsyncSession, session_id: UUID) -> None:
    """FOR KEY SHARE on the session row: always the first lock a node writer takes."""
    stmt = select(literal(1)).where(SessionRow.id == session_id).with_for_update(key_share=True)
    if (await db.execute(stmt)).scalar_one_or_none() is None:
        raise NotFoundError(f"session {session_id} not found")


async def _step_session_id(db: AsyncSession, step_id: UUID) -> UUID:
    session_id = (
        await db.execute(select(StepRow.session_id).where(StepRow.id == step_id))
    ).scalar_one_or_none()
    if session_id is None:
        raise NotFoundError(f"step {step_id} not found")
    return session_id


async def _check_parent(db: AsyncSession, step: Step, node: GraphNode) -> None:
    """The parent must be the step's parent node (the root for the Overview step) or a node
    of the same step."""
    parent_id = node.parent_id
    if parent_id == node.id:
        raise StoreError("a node cannot be its own parent")
    if parent_id is None:
        raise StoreError("a step node needs a parent")
    if step.parent_node_id is not None and parent_id == step.parent_node_id:
        return
    stmt = select(GraphNodeRow.step_id, GraphNodeRow.kind).where(
        GraphNodeRow.id == parent_id, GraphNodeRow.session_id == step.session_id
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is not None and (
        row.step_id == step.id or (step.parent_node_id is None and row.kind == "root")
    ):
        return
    raise StoreError("the parent must be the step's parent node or a node of the same step")


async def _lock_open_step(db: AsyncSession, step_id: UUID) -> Step:
    stmt = select(*StepRow.__table__.c).where(StepRow.id == step_id).with_for_update()
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise NotFoundError(f"step {step_id} not found")
    step = _step(row)
    if step.status not in OPEN_STEP_STATUSES:
        raise StepClosedError(f"step {step_id} is already closed ({step.status})")
    return step


async def _finish_step(db: AsyncSession, step_id: UUID, status: StepStatus) -> Step:
    stmt = (
        update(StepRow)
        .where(StepRow.id == step_id, StepRow.status.in_(OPEN_STEP_STATUSES))
        .values(status=status, closed_at=func.clock_timestamp())
        .returning(*StepRow.__table__.c)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise StepClosedError(f"step {step_id} is already closed")
    return _step(row)


async def _close(
    db: AsyncSession, step_id: UUID, status: StepStatus, node: GraphNode
) -> tuple[Step, GraphNode]:
    if status not in CLOSED_STEP_STATUSES:
        raise StoreError(f"cannot close a step as {status!r}")
    if node.step_id != step_id:
        raise StoreError("the final node belongs to another step")
    if node.kind not in FINAL_NODE_KINDS:
        raise StoreError(f"a step must end in a waypoint or error node, not {node.kind!r}")
    await _lock_session(db, node.session_id)
    step = await _lock_open_step(db, step_id)
    if node.session_id != step.session_id:
        raise StoreError("the final node belongs to another session")
    await _check_parent(db, step, node)
    stored = await _insert_node(db, node)
    return await _finish_step(db, step_id, status), stored


async def close_step(
    db: AsyncSession, step_id: UUID, status: StepStatus, final_node: GraphNode
) -> tuple[Step, GraphNode]:
    """Insert the step's final node and close the step, in one transaction.

    The step may be queued or running. Refuses a step that is already closed
    (StepClosedError); then nothing is written. Returns the closed step and the node as stored.
    """
    return await _write(db, lambda: _close(db, step_id, status, final_node))


async def open_steps(db: AsyncSession) -> list[Step]:
    stmt = (
        select(*StepRow.__table__.c)
        .where(StepRow.status.in_(OPEN_STEP_STATUSES))
        .order_by(StepRow.created_at, StepRow.id)
    )
    return await _read(db, lambda: _all(db, stmt, _step))


async def mark_interrupted(db: AsyncSession, step_id: UUID) -> tuple[Step, GraphNode]:
    """Close an open step as interrupted with an error node (startup recovery), atomically.

    Accepts a queued or running step. The error node hangs off the step's latest node, or the
    step's parent node if it has none.
    """

    async def work() -> tuple[Step, GraphNode]:
        await _lock_session(db, await _step_session_id(db, step_id))
        step = await _lock_open_step(db, step_id)
        last = (
            await db.execute(
                select(GraphNodeRow.id)
                .where(GraphNodeRow.step_id == step_id)
                .order_by(*(c.desc() for c in _NODE_ORDER))
                .limit(1)
            )
        ).scalar_one_or_none()
        parent = last if last is not None else step.parent_node_id
        if parent is None:  # the Overview step hangs off the root
            parent = (
                await db.execute(
                    select(GraphNodeRow.id).where(
                        GraphNodeRow.session_id == step.session_id, GraphNodeRow.kind == "root"
                    )
                )
            ).scalar_one_or_none()
        node = GraphNode(
            id=uuid4(),
            session_id=step.session_id,
            step_id=step.id,
            parent_id=parent,
            kind="error",
            mode=step.mode,
            error=INTERRUPTED_ERROR,
            model=step.model,
            created_at=datetime.now(UTC),  # replaced by the DB clock on insert
        )
        return await _close(db, step_id, "interrupted", node)

    return await _write(db, work)


# --- nodes ---------------------------------------------------------------------------


async def append_node(db: AsyncSession, node: GraphNode) -> GraphNode:
    """Insert one node of a running step and return it as stored.

    Root nodes are refused (create_session makes the only one). The step row is locked
    FOR SHARE, so a concurrent close_step (FOR UPDATE) waits and no node lands after it.
    A caller-supplied created_at is ignored: the DB clock sets it.
    """
    if node.kind == "root" or node.step_id is None:
        raise StoreError("append_node takes step nodes only; create_session makes the root")
    step_id = node.step_id

    async def work() -> GraphNode:
        await _lock_session(db, node.session_id)
        stmt = select(*StepRow.__table__.c).where(StepRow.id == step_id).with_for_update(read=True)
        row = (await db.execute(stmt)).one_or_none()
        if row is None:
            raise NotFoundError(f"step {step_id} not found")
        step = _step(row)
        if step.session_id != node.session_id:
            raise StoreError("the node belongs to another session than its step")
        if step.status != "running":
            raise StepStateError(f"step {step_id} is {step.status}, not running")
        await _check_parent(db, step, node)
        return await _insert_node(db, node)

    return await _write(db, work)


async def load_nodes(db: AsyncSession, session_id: UUID) -> list[GraphNode]:
    stmt = (
        select(*GraphNodeRow.__table__.c)
        .where(GraphNodeRow.session_id == session_id)
        .order_by(*_NODE_ORDER)
    )
    return await _read(db, lambda: _all(db, stmt, _node))


async def step_nodes(db: AsyncSession, step_id: UUID) -> list[GraphNode]:
    stmt = (
        select(*GraphNodeRow.__table__.c)
        .where(GraphNodeRow.step_id == step_id)
        .order_by(*_NODE_ORDER)
    )
    return await _read(db, lambda: _all(db, stmt, _node))


async def children(db: AsyncSession, node_id: UUID) -> list[GraphNode]:
    stmt = (
        select(*GraphNodeRow.__table__.c)
        .where(GraphNodeRow.parent_id == node_id)
        .order_by(*_NODE_ORDER)
    )
    return await _read(db, lambda: _all(db, stmt, _node))


_NODE_COLUMNS = ", ".join(c.name for c in GraphNodeRow.__table__.c)

# CYCLE marks the first repeated node, so a corrupt parent chain ends the walk instead of
# recursing forever; the parent CHECK and FKs should make that impossible.
# Column names come from the model metadata, never from input: S608 does not apply.
_ROOT_PATH_SQL = text(
    f"""
    WITH RECURSIVE path AS (
        SELECT {_NODE_COLUMNS}, 0 AS depth
        FROM graph_nodes WHERE id = :node_id
      UNION ALL
        SELECT {", ".join(f"p.{c.name}" for c in GraphNodeRow.__table__.c)}, path.depth + 1
        FROM graph_nodes AS p
        JOIN path ON p.id = path.parent_id AND p.session_id = path.session_id
    ) CYCLE id SET is_cycle USING visited
    SELECT {_NODE_COLUMNS}, is_cycle FROM path ORDER BY depth DESC
    """  # noqa: S608
).columns(*GraphNodeRow.__table__.c, column("is_cycle", Boolean))


async def root_path(db: AsyncSession, node_id: UUID) -> list[GraphNode]:
    """The nodes from the root down to `node_id` (inclusive). Empty if the node is unknown.

    Walks parent_id upwards in a recursive CTE, never leaving the node's session, so sibling
    branches can never appear. Raises StoreError if the parent chain has a cycle.
    """

    async def work() -> list[GraphNode]:
        rows = (await db.execute(_ROOT_PATH_SQL, {"node_id": node_id})).all()
        if any(row.is_cycle for row in rows):
            raise StoreError(f"the parent chain of node {node_id} has a cycle")
        return [
            GraphNode.model_validate({k: v for k, v in row._mapping.items() if k != "is_cycle"})
            for row in rows
        ]

    return await _read(db, work)


# --- purge ---------------------------------------------------------------------------


async def purge(db: AsyncSession, owner_id: UUID, session_ids: Sequence[UUID]) -> int:
    """Delete whole sessions (nodes, steps, session rows) owned by `owner_id`.

    Goes through purge_sessions(), the only way to remove graph rows. Sessions of another
    owner are left alone. Returns the number of sessions removed.
    """
    stmt = select(
        func.purge_sessions(
            literal(owner_id, PG_UUID()), literal(list(session_ids), ARRAY(PG_UUID()))
        )
    )

    async def work() -> int:
        return int((await db.execute(stmt)).scalar_one())

    try:
        return await _write(db, work)
    except DBAPIError as exc:
        if _sqlstate(exc) != DEADLOCK_DETECTED:
            raise
    # Lock order should prevent this; one retry covers a writer outside the store API.
    return await _write(db, work)


def _sqlstate(exc: DBAPIError) -> str | None:
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        code = getattr(err, "sqlstate", None)
        if isinstance(code, str):
            return code
    return None


__all__ = [
    "CLOSED_STEP_STATUSES",
    "INTERRUPTED_ERROR",
    "OPEN_STEP_STATUSES",
    "NotFoundError",
    "StepClosedError",
    "StepStateError",
    "StoreError",
    "append_node",
    "children",
    "close_step",
    "create_session",
    "create_step",
    "load_nodes",
    "mark_interrupted",
    "open_steps",
    "purge",
    "root_path",
    "set_session_status",
    "start_step",
    "step_nodes",
]

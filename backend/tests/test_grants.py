"""Role grants from the 0001 migration, checked as ahoy_app against the test database (T36)."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.store import repo

pytestmark = pytest.mark.db

MakeUser = Callable[[], Awaitable[UUID]]


async def scalar(db: AsyncSession, sql: str, **params: Any) -> Any:
    return (await db.execute(text(sql), params)).scalar_one()


async def denied(db: AsyncSession, sql: str, **params: Any) -> None:
    """`sql` fails with permission denied; the savepoint keeps the test transaction usable."""
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db.begin_nested():
            await db.execute(text(sql), params)


async def graph(db: AsyncSession, owner: UUID) -> tuple[UUID, UUID, UUID]:
    """A session with a root, one closed step and its waypoint. Returns (session, step, node)."""
    session, root = await repo.create_session(
        db,
        owner_id=owner,
        installation_id=1,
        repo_id=2,
        repo_full_name="o/r",
        ref="main",
        commit_sha="b" * 40,
        private=True,
        model="m",
    )
    root_id, node_id = root.id, uuid4()
    step = await repo.create_step(
        db,
        session_id=session.id,
        parent_node_id=root_id,
        steered_by=owner,
        wedge="w",
        instruction="i",
        mode="safe",
        provider="mock",
        model="m",
    )
    await db.execute(
        text(
            "INSERT INTO graph_nodes (id, session_id, step_id, parent_id, kind, error, model)"
            " VALUES (:id, :sid, :step, :root, 'error', 'boom', 'm')"
        ),
        {"id": node_id, "sid": session.id, "step": step.id, "root": root_id},
    )
    return session.id, step.id, node_id


async def counts(db: AsyncSession, session_id: UUID) -> tuple[int, int, int]:
    p = {"sid": session_id}
    return (
        await scalar(db, "SELECT count(*) FROM sessions WHERE id = :sid", **p),
        await scalar(db, "SELECT count(*) FROM steps WHERE session_id = :sid", **p),
        await scalar(db, "SELECT count(*) FROM graph_nodes WHERE session_id = :sid", **p),
    )


async def test_connected_as_ahoy_app(db: AsyncSession) -> None:
    assert await scalar(db, "SELECT current_user") == "ahoy_app"


async def test_graph_nodes_are_append_only(db: AsyncSession, make_user: MakeUser) -> None:
    _, _, node_id = await graph(db, await make_user())
    await denied(db, "UPDATE graph_nodes SET model = 'x' WHERE id = :id", id=node_id)
    await denied(db, "DELETE FROM graph_nodes WHERE id = :id", id=node_id)
    await denied(db, "TRUNCATE graph_nodes")
    assert await scalar(db, "SELECT model FROM graph_nodes WHERE id = :id", id=node_id) == "m"


async def test_steps_and_sessions_cannot_be_deleted(db: AsyncSession, make_user: MakeUser) -> None:
    session_id, step_id, _ = await graph(db, await make_user())
    await denied(db, "DELETE FROM steps WHERE id = :id", id=step_id)
    await denied(db, "DELETE FROM sessions WHERE id = :id", id=session_id)
    await denied(db, "TRUNCATE steps, sessions CASCADE")
    assert await counts(db, session_id) == (1, 1, 2)


async def test_keys_and_owners_cannot_be_updated(db: AsyncSession, make_user: MakeUser) -> None:
    other = await make_user()
    session_id, step_id, _ = await graph(db, await make_user())
    await denied(db, "UPDATE steps SET session_id = session_id WHERE id = :id", id=step_id)
    await denied(db, "UPDATE sessions SET owner_id = :o WHERE id = :id", o=other, id=session_id)
    await denied(db, "UPDATE sessions SET repo_id = 3 WHERE id = :id", id=session_id)
    await denied(db, "UPDATE sessions SET commit_sha = 'c' WHERE id = :id", id=session_id)
    await denied(db, "UPDATE users SET github_id = 5 WHERE id = :id", id=other)


async def test_allowed_column_updates(db: AsyncSession, make_user: MakeUser) -> None:
    user = await make_user()
    session_id, step_id, _ = await graph(db, user)
    await db.execute(
        text("UPDATE steps SET status = 'running', started_at = now() WHERE id = :id"),
        {"id": step_id},
    )
    await db.execute(
        text("UPDATE steps SET status = 'done', closed_at = now() WHERE id = :id"),
        {"id": step_id},
    )
    await db.execute(
        text(
            "UPDATE sessions SET status = 'failed', status_reason = 'r', shared = true"
            " WHERE id = :id"
        ),
        {"id": session_id},
    )
    await db.execute(
        text(
            "UPDATE users SET github_login = 'n', avatar_url = 'a', settings = '{}'::jsonb,"
            " private_repo_notice_ack_at = now() WHERE id = :id"
        ),
        {"id": user},
    )
    assert await scalar(db, "SELECT status FROM steps WHERE id = :id", id=step_id) == "done"
    assert await scalar(db, "SELECT shared FROM sessions WHERE id = :id", id=session_id) is True


async def test_purge_removes_whole_sessions(db: AsyncSession, make_user: MakeUser) -> None:
    owner = await make_user()
    first, _, _ = await graph(db, owner)
    second, _, _ = await graph(db, owner)
    kept, _, _ = await graph(db, owner)

    assert await repo.purge(db, owner, [first, second]) == 2
    assert await counts(db, first) == (0, 0, 0)
    assert await counts(db, second) == (0, 0, 0)
    assert await counts(db, kept) == (1, 1, 2)


async def test_purge_refuses_another_owners_sessions(db: AsyncSession, make_user: MakeUser) -> None:
    owner, intruder = await make_user(), await make_user()
    session_id, _, _ = await graph(db, owner)
    assert await repo.purge(db, intruder, [session_id]) == 0
    assert await counts(db, session_id) == (1, 1, 2)


async def test_deleting_a_user_needs_purge_first(db: AsyncSession, make_user: MakeUser) -> None:
    owner = await make_user()
    session_id, _, _ = await graph(db, owner)

    with pytest.raises(IntegrityError, match="fk_sessions_owner_id_users"):
        async with db.begin_nested():
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner})
    assert await counts(db, session_id) == (1, 1, 2)

    assert await repo.purge(db, owner, [session_id]) == 1
    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner})
    assert await scalar(db, "SELECT count(*) FROM users WHERE id = :id", id=owner) == 0

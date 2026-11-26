"""Store functions for GitHub webhook deliveries (SPEC -> GitHub integration, Webhooks).

Same conventions as store/repo.py: every call is one transaction that commits, or rolls back
and re-raises. A delivery id is recorded in the same transaction as the delivery's database
side effects, so a delivery whose handling fails is not marked seen and GitHub's redelivery
is processed again. `webhook_deliveries` is insert-only (no UPDATE grant for ahoy_app).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ahoy.store.auth import delete_user_auth_rows, user_by_github_id
from ahoy.store.models import WebhookDeliveryRow
from ahoy.store.repo import _write


async def _claim(db: AsyncSession, delivery_id: str) -> bool:
    """Insert the delivery id; False if it was already there. No commit."""
    stmt = (
        pg_insert(WebhookDeliveryRow)
        .values(delivery_id=delivery_id, received_at=func.clock_timestamp())
        .on_conflict_do_nothing(index_elements=[WebhookDeliveryRow.delivery_id])
        .returning(WebhookDeliveryRow.delivery_id)
    )
    return (await db.execute(stmt)).first() is not None


async def record_delivery(db: AsyncSession, delivery_id: str) -> bool:
    """Record a delivery that has no database side effects. True if it is new."""
    return await _write(db, lambda: _claim(db, delivery_id))


@dataclass(frozen=True)
class Revocation:
    new: bool  # False: a replayed delivery, nothing was done
    user_id: UUID | None  # the signed-out user, or None if no user has that GitHub id


async def record_revocation(db: AsyncSession, delivery_id: str, github_id: int) -> Revocation:
    """Record the delivery and, if new, delete the tokens and login sessions of the user with
    this numeric GitHub id, in one transaction."""

    async def work() -> Revocation:
        if not await _claim(db, delivery_id):
            return Revocation(new=False, user_id=None)
        user = await user_by_github_id(db, github_id)
        if user is None:
            return Revocation(new=True, user_id=None)
        await delete_user_auth_rows(db, user.id)
        return Revocation(new=True, user_id=user.id)

    return await _write(db, work)

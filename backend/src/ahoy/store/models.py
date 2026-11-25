"""SQLAlchemy models (SPEC -> Data model). They mirror the Pydantic models in harness/schemas.py.

Column names match the Pydantic field names. GitHubTokens and AuthSession are DB-only.
Graph nodes are append-only: the `ahoy_app` role has no UPDATE or DELETE on `graph_nodes`
(grants are in the 0001 migration), and the only way to remove rows is `purge_sessions()`.

Foreign keys into `sessions`, `steps` and `graph_nodes` have no ON DELETE action on purpose:
a cascade runs with the table owner's rights and would let `ahoy_app` delete nodes indirectly
(for example by deleting a user). Graph rows reference their parents through composite keys
`(session_id, id)` so a node, step or parent can never point into another session.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# AES-256-GCM nonce and SHA-256 digest sizes, in bytes.
GCM_NONCE_BYTES = 12
SHA256_BYTES = 32

_UUID_DEFAULT = text("gen_random_uuid()")


def _tz(nullable: bool = False, *, now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if now else None,
    )


def _jsonb() -> Any:
    # none_as_null: Python None is SQL NULL, so the CHECK constraints see it as absent.
    return mapped_column(JSONB(none_as_null=True), nullable=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    github_login: Mapped[str] = mapped_column(Text)
    avatar_url: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    private_repo_notice_ack_at: Mapped[datetime | None] = _tz(nullable=True)
    created_at: Mapped[datetime] = _tz(now=True)


class GitHubTokensRow(Base):
    """GitHub user tokens, AES-256-GCM encrypted. Never in the API, never plaintext."""

    __tablename__ = "github_tokens"
    __table_args__ = (
        CheckConstraint(
            f"octet_length(access_token_nonce) = {GCM_NONCE_BYTES}", name="access_nonce_len"
        ),
        CheckConstraint(
            f"octet_length(refresh_token_nonce) = {GCM_NONCE_BYTES}", name="refresh_nonce_len"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    access_expires_at: Mapped[datetime] = _tz()
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_expires_at: Mapped[datetime] = _tz()


class AuthSessionRow(Base):
    """A signed-in browser. Only the SHA-256 of the cookie value is stored."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint(f"octet_length(id_hash) = {SHA256_BYTES}", name="id_hash_len"),
    )

    id_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = _tz(now=True)
    last_seen_at: Mapped[datetime] = _tz(now=True)
    user_agent: Mapped[str] = mapped_column(Text)


class SessionRow(Base):
    """A knowledge tree."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("status IN ('preparing', 'ready', 'failed')", name="status"),
        Index("ix_sessions_owner_id_created_at", "owner_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_id: Mapped[int] = mapped_column(BigInteger, index=True)
    repo_full_name: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str] = mapped_column(Text)
    private: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(Text)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    shared: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _tz(now=True)


class StepRow(Base):
    """One steer is one step; the only mutable graph-side row."""

    __tablename__ = "steps"
    __table_args__ = (
        CheckConstraint("mode IN ('read_only', 'safe', 'auto')", name="mode"),
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error', 'interrupted')", name="status"
        ),
        UniqueConstraint("session_id", "id"),
        # Circular with graph_nodes, so it is added after both tables exist.
        ForeignKeyConstraint(
            ["session_id", "parent_node_id"],
            ["graph_nodes.session_id", "graph_nodes.id"],
            name="fk_steps_session_id_parent_node_id_graph_nodes",
            use_alter=True,
        ),
        # Open steps (startup recovery, running-step quotas).
        Index(
            "ix_steps_open",
            "status",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        # Daily step quota per user.
        Index("ix_steps_steered_by_created_at", "steered_by", "created_at"),
        # At most one Overview step (the only step without a parent node) per session.
        Index(
            "uq_steps_one_overview",
            "session_id",
            unique=True,
            postgresql_where=text("parent_node_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    parent_node_id: Mapped[UUID | None] = mapped_column(nullable=True)
    steered_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    wedge: Mapped[str] = mapped_column(Text)
    instruction: Mapped[str] = mapped_column(Text)
    allow_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _tz(now=True)
    started_at: Mapped[datetime | None] = _tz(nullable=True)
    closed_at: Mapped[datetime | None] = _tz(nullable=True)


class GraphNodeRow(Base):
    """An append-only graph node. CHECKs mirror the GraphNode validator."""

    __tablename__ = "graph_nodes"
    __table_args__ = (
        CheckConstraint("kind IN ('root', 'tool', 'blocked', 'waypoint', 'error')", name="kind"),
        CheckConstraint("mode IS NULL OR mode IN ('read_only', 'safe', 'auto')", name="mode"),
        CheckConstraint("(kind = 'root') = (step_id IS NULL)", name="step_id_iff_not_root"),
        # The parent FK is satisfied by the row itself, so only this stops a self-loop.
        CheckConstraint("parent_id IS DISTINCT FROM id", name="parent_not_self"),
        CheckConstraint("(kind = 'tool') = (tool IS NOT NULL)", name="tool_iff_tool"),
        CheckConstraint("(kind = 'blocked') = (blocked IS NOT NULL)", name="blocked_iff_blocked"),
        CheckConstraint(
            "(kind = 'waypoint') = (waypoint IS NOT NULL)", name="waypoint_iff_waypoint"
        ),
        CheckConstraint("steer IS NULL OR jsonb_typeof(steer) = 'object'", name="steer_is_object"),
        CheckConstraint("tool IS NULL OR jsonb_typeof(tool) = 'object'", name="tool_is_object"),
        CheckConstraint(
            "blocked IS NULL OR jsonb_typeof(blocked) = 'object'", name="blocked_is_object"
        ),
        CheckConstraint(
            "waypoint IS NULL OR jsonb_typeof(waypoint) = 'object'", name="waypoint_is_object"
        ),
        CheckConstraint(
            r"(kind = 'error' AND error IS NOT NULL AND error ~ '\S')"
            " OR (kind <> 'error' AND error IS NULL)",
            name="error_iff_error",
        ),
        UniqueConstraint("session_id", "id"),
        ForeignKeyConstraint(
            ["session_id", "step_id"],
            ["steps.session_id", "steps.id"],
            name="fk_graph_nodes_session_id_step_id_steps",
        ),
        ForeignKeyConstraint(
            ["session_id", "parent_id"],
            ["graph_nodes.session_id", "graph_nodes.id"],
            name="fk_graph_nodes_session_id_parent_id_graph_nodes",
        ),
        Index("ix_graph_nodes_session_id_created_at", "session_id", "created_at"),
        # At most one root per session.
        Index(
            "uq_graph_nodes_one_root",
            "session_id",
            unique=True,
            postgresql_where=text("kind = 'root'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id"))
    step_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    parent_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    kind: Mapped[str] = mapped_column(Text)
    steer: Mapped[dict[str, Any] | None] = _jsonb()
    mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool: Mapped[dict[str, Any] | None] = _jsonb()
    blocked: Mapped[dict[str, Any] | None] = _jsonb()
    waypoint: Mapped[dict[str, Any] | None] = _jsonb()
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _tz(now=True)


class WebhookDeliveryRow(Base):
    """GitHub webhook deliveries seen, for deduplication by X-GitHub-Delivery."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    received_at: Mapped[datetime] = _tz(now=True)

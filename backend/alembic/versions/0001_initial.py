"""Initial schema: tables, role grants and purge_sessions().

Run as ahoy_owner (MIGRATION_DATABASE_URL). The roles themselves come from
deploy/db/bootstrap.sh. Grants follow SPEC -> Data model -> Database roles, with UPDATE
narrowed to the columns the app changes (see APP_UPDATE_COLUMNS); ahoy_app gets no UPDATE
or DELETE on graph_nodes (append-only).

No ON DELETE CASCADE on any key into sessions, steps or graph_nodes. Referential actions run
with the table owner's rights, so a cascade would let ahoy_app delete graph rows indirectly
(for example by deleting a user) and bypass the append-only grants. purge_sessions() is the
only way to remove them. Account deletion must therefore call purge_sessions() for all of the
user's sessions before deleting the users row; deleting a user who still owns sessions fails
on fk_sessions_owner_id_users. (github_tokens and auth_sessions do cascade from users: ahoy_app
may delete those rows anyway.)

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_TZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB(none_as_null=True)
_NOW = sa.text("now()")
_GEN_UUID = sa.text("gen_random_uuid()")

# The only columns ahoy_app may UPDATE. Keys, owners and pinned values (ids, owner_id,
# session_id, repo_id, commit_sha, github_id, ...) are never updatable.
APP_UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    # Step lifecycle: queued -> running -> done | error | interrupted.
    "steps": ("status", "started_at", "closed_at"),
    # Preparation outcome and the owner's sharing toggle.
    "sessions": ("status", "status_reason", "shared"),
    # Profile refreshed at sign-in, PUT /api/me/settings, private-repo notice.
    "users": ("github_login", "avatar_url", "settings", "private_repo_notice_ack_at"),
    # Token refresh rotates both tokens in one transaction.
    "github_tokens": (
        "access_token_enc",
        "access_token_nonce",
        "access_expires_at",
        "refresh_token_enc",
        "refresh_token_nonce",
        "refresh_expires_at",
    ),
    "auth_sessions": ("last_seen_at",),
    # webhook_deliveries: insert-only dedupe (ON CONFLICT DO NOTHING), no UPDATE.
}

GRANTS = (
    "GRANT SELECT, INSERT ON graph_nodes, steps, sessions TO ahoy_app",
    "GRANT SELECT, INSERT, DELETE"
    " ON users, github_tokens, auth_sessions, webhook_deliveries TO ahoy_app",
    *(
        f"GRANT UPDATE ({', '.join(cols)}) ON {table} TO ahoy_app"
        for table, cols in APP_UPDATE_COLUMNS.items()
    ),
    "GRANT SELECT, DELETE ON graph_nodes, steps, sessions TO ahoy_purge",
    # purge_sessions() locks the target sessions with SELECT ... FOR UPDATE, the only row
    # lock mode that conflicts with the FOR KEY SHARE locks that in-flight node and step
    # inserts take through their foreign keys. Postgres requires UPDATE privilege (on at
    # least one column) for row locks. The function never updates; one non-key column is
    # the minimum.
    "GRANT UPDATE (status) ON sessions TO ahoy_purge",
)

# Parameters are qualified with the function name because they share names with columns.
# All deletes are one statement, so the circular steps <-> graph_nodes keys are checked
# only once every row of the purged sessions is gone.
PURGE_SESSIONS = r"""
CREATE FUNCTION purge_sessions(owner_id uuid, session_ids uuid[])
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    purged integer;
BEGIN
    -- Wait for in-flight node and step inserts (they hold FOR KEY SHARE on the session row
    -- through their foreign keys). The DELETE below then takes a fresh snapshot that sees
    -- their rows, instead of failing on the foreign key at the end of the statement.
    PERFORM 1
    FROM public.sessions AS s
    WHERE s.owner_id = purge_sessions.owner_id
      AND s.id = ANY (purge_sessions.session_ids)
    FOR UPDATE;

    WITH targets AS (
        SELECT s.id
        FROM public.sessions AS s
        WHERE s.owner_id = purge_sessions.owner_id
          AND s.id = ANY (purge_sessions.session_ids)
    ),
    deleted_nodes AS (
        DELETE FROM public.graph_nodes AS n
        USING targets AS t
        WHERE n.session_id = t.id
    ),
    deleted_steps AS (
        DELETE FROM public.steps AS st
        USING targets AS t
        WHERE st.session_id = t.id
    ),
    deleted_sessions AS (
        DELETE FROM public.sessions AS s
        USING targets AS t
        WHERE s.id = t.id
        RETURNING s.id
    )
    SELECT count(*) INTO purged FROM deleted_sessions;
    RETURN purged;
END;
$$
"""

_PURGE_SIG = "purge_sessions(uuid, uuid[])"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", _UUID, nullable=False, server_default=_GEN_UUID),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("private_repo_notice_ack_at", _TZ, nullable=True),
        sa.Column("created_at", _TZ, nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("github_id", name="uq_users_github_id"),
    )

    op.create_table(
        "github_tokens",
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("access_token_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("access_expires_at", _TZ, nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_expires_at", _TZ, nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_github_tokens"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_github_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "octet_length(access_token_nonce) = 12", name=op.f("ck_github_tokens_access_nonce_len")
        ),
        sa.CheckConstraint(
            "octet_length(refresh_token_nonce) = 12",
            name=op.f("ck_github_tokens_refresh_nonce_len"),
        ),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("created_at", _TZ, nullable=False, server_default=_NOW),
        sa.Column("last_seen_at", _TZ, nullable=False, server_default=_NOW),
        sa.Column("user_agent", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id_hash", name="pk_auth_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("octet_length(id_hash) = 32", name=op.f("ck_auth_sessions_id_hash_len")),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "sessions",
        sa.Column("id", _UUID, nullable=False, server_default=_GEN_UUID),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", _TZ, nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name="fk_sessions_owner_id_users"),
        sa.CheckConstraint(
            "status IN ('preparing', 'ready', 'failed')", name=op.f("ck_sessions_status")
        ),
    )
    op.create_index("ix_sessions_owner_id_created_at", "sessions", ["owner_id", "created_at"])
    op.create_index("ix_sessions_repo_id", "sessions", ["repo_id"])

    op.create_table(
        "steps",
        sa.Column("id", _UUID, nullable=False, server_default=_GEN_UUID),
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("parent_node_id", _UUID, nullable=True),
        sa.Column("steered_by", _UUID, nullable=False),
        sa.Column("wedge", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("allow_command", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False, server_default=_NOW),
        sa.Column("started_at", _TZ, nullable=True),
        sa.Column("closed_at", _TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_steps"),
        sa.UniqueConstraint("session_id", "id", name="uq_steps_session_id_id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name="fk_steps_session_id_sessions"
        ),
        sa.ForeignKeyConstraint(["steered_by"], ["users.id"], name="fk_steps_steered_by_users"),
        sa.CheckConstraint("mode IN ('read_only', 'safe', 'auto')", name=op.f("ck_steps_mode")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error', 'interrupted')",
            name=op.f("ck_steps_status"),
        ),
    )
    op.create_index("ix_steps_session_id", "steps", ["session_id"])
    op.create_index(
        "ix_steps_open",
        "steps",
        ["status"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_steps_steered_by_created_at", "steps", ["steered_by", "created_at"])
    # At most one Overview step (the only step without a parent node) per session.
    op.create_index(
        "uq_steps_one_overview",
        "steps",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("parent_node_id IS NULL"),
    )

    op.create_table(
        "graph_nodes",
        sa.Column("id", _UUID, nullable=False, server_default=_GEN_UUID),
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("step_id", _UUID, nullable=True),
        sa.Column("parent_id", _UUID, nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("steer", _JSONB, nullable=True),
        sa.Column("mode", sa.Text(), nullable=True),
        sa.Column("tool", _JSONB, nullable=True),
        sa.Column("blocked", _JSONB, nullable=True),
        sa.Column("waypoint", _JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_graph_nodes"),
        sa.UniqueConstraint("session_id", "id", name="uq_graph_nodes_session_id_id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name="fk_graph_nodes_session_id_sessions"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "step_id"],
            ["steps.session_id", "steps.id"],
            name="fk_graph_nodes_session_id_step_id_steps",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "parent_id"],
            ["graph_nodes.session_id", "graph_nodes.id"],
            name="fk_graph_nodes_session_id_parent_id_graph_nodes",
        ),
        sa.CheckConstraint(
            "kind IN ('root', 'tool', 'blocked', 'waypoint', 'error')",
            name=op.f("ck_graph_nodes_kind"),
        ),
        sa.CheckConstraint(
            "mode IS NULL OR mode IN ('read_only', 'safe', 'auto')",
            name=op.f("ck_graph_nodes_mode"),
        ),
        sa.CheckConstraint(
            "(kind = 'root') = (step_id IS NULL)", name=op.f("ck_graph_nodes_step_id_iff_not_root")
        ),
        # The parent FK is satisfied by the row itself, so only this stops a self-loop.
        sa.CheckConstraint(
            "parent_id IS DISTINCT FROM id", name=op.f("ck_graph_nodes_parent_not_self")
        ),
        sa.CheckConstraint(
            "(kind = 'tool') = (tool IS NOT NULL)", name=op.f("ck_graph_nodes_tool_iff_tool")
        ),
        sa.CheckConstraint(
            "(kind = 'blocked') = (blocked IS NOT NULL)",
            name=op.f("ck_graph_nodes_blocked_iff_blocked"),
        ),
        sa.CheckConstraint(
            "(kind = 'waypoint') = (waypoint IS NOT NULL)",
            name=op.f("ck_graph_nodes_waypoint_iff_waypoint"),
        ),
        sa.CheckConstraint(
            "steer IS NULL OR jsonb_typeof(steer) = 'object'",
            name=op.f("ck_graph_nodes_steer_is_object"),
        ),
        sa.CheckConstraint(
            "tool IS NULL OR jsonb_typeof(tool) = 'object'",
            name=op.f("ck_graph_nodes_tool_is_object"),
        ),
        sa.CheckConstraint(
            "blocked IS NULL OR jsonb_typeof(blocked) = 'object'",
            name=op.f("ck_graph_nodes_blocked_is_object"),
        ),
        sa.CheckConstraint(
            "waypoint IS NULL OR jsonb_typeof(waypoint) = 'object'",
            name=op.f("ck_graph_nodes_waypoint_is_object"),
        ),
        sa.CheckConstraint(
            r"(kind = 'error' AND error IS NOT NULL AND error ~ '\S')"
            " OR (kind <> 'error' AND error IS NULL)",
            name=op.f("ck_graph_nodes_error_iff_error"),
        ),
    )
    op.create_index(
        "ix_graph_nodes_session_id_created_at", "graph_nodes", ["session_id", "created_at"]
    )
    op.create_index("ix_graph_nodes_parent_id", "graph_nodes", ["parent_id"])
    op.create_index("ix_graph_nodes_step_id", "graph_nodes", ["step_id"])
    op.create_index(
        "uq_graph_nodes_one_root",
        "graph_nodes",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'root'"),
    )

    # steps <-> graph_nodes is circular; add the step's parent-node key last.
    op.create_foreign_key(
        "fk_steps_session_id_parent_node_id_graph_nodes",
        "steps",
        "graph_nodes",
        ["session_id", "parent_node_id"],
        ["session_id", "id"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("received_at", _TZ, nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("delivery_id", name="pk_webhook_deliveries"),
    )

    for statement in GRANTS:
        op.execute(statement)

    op.execute(PURGE_SESSIONS)
    # Handing the function to ahoy_purge needs ahoy_owner to be a member of it (bootstrap
    # grants that) and ahoy_purge to have CREATE on the schema, granted only for this step.
    op.execute("GRANT CREATE ON SCHEMA public TO ahoy_purge")
    op.execute(f"ALTER FUNCTION {_PURGE_SIG} OWNER TO ahoy_purge")
    op.execute("REVOKE CREATE ON SCHEMA public FROM ahoy_purge")
    op.execute(f"REVOKE ALL ON FUNCTION {_PURGE_SIG} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PURGE_SIG} TO ahoy_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_PURGE_SIG}")
    op.drop_table("webhook_deliveries")
    op.drop_constraint(
        "fk_steps_session_id_parent_node_id_graph_nodes", "steps", type_="foreignkey"
    )
    op.drop_table("graph_nodes")
    op.drop_table("steps")
    op.drop_table("sessions")
    op.drop_table("auth_sessions")
    op.drop_table("github_tokens")
    op.drop_table("users")

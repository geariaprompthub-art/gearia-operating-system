"""Create authentication sessions and refresh-token rotation records.

Revision ID: 20260728_0008
Revises: 20260727_0007
"""

from alembic import op
import sqlalchemy as sa

revision = "20260728_0008"
down_revision = "20260727_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add only P1B authentication persistence objects."""
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("csrf_secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("revocation_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry"),
        sa.CheckConstraint("token_version >= 1", name="ck_auth_sessions_token_version"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_active", "auth_sessions", ["user_id", "expires_at"], postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_table(
        "auth_refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("parent_token_id", sa.Uuid(), nullable=True),
        sa.Column("replaced_by_token_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reuse_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["auth_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_token_id"], ["auth_refresh_tokens.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["replaced_by_token_id"], ["auth_refresh_tokens.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("token_hash", name="uq_auth_refresh_tokens_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_refresh_tokens_expiry"),
    )
    op.create_index("ix_auth_refresh_tokens_session_id", "auth_refresh_tokens", ["session_id"])
    op.create_index("ix_auth_refresh_tokens_family_id", "auth_refresh_tokens", ["family_id"])
    op.create_index("ix_auth_refresh_tokens_expires_at", "auth_refresh_tokens", ["expires_at"])


def downgrade() -> None:
    """Remove only P1B authentication objects."""
    op.drop_table("auth_refresh_tokens")
    op.drop_table("auth_sessions")

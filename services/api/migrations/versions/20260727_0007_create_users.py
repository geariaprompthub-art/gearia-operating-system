"""Create internal users.

Revision ID: 20260727_0007
Revises: 20260721_0006
"""

from alembic import op
import sqlalchemy as sa

revision = "20260727_0007"
down_revision = "20260721_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the internal identity table and its database invariants."""
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending_verification",
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "failed_login_count", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
        sa.CheckConstraint("token_version >= 1", name="ck_users_token_version"),
        sa.CheckConstraint(
            "failed_login_count >= 0", name="ck_users_failed_login_count"
        ),
        sa.CheckConstraint(
            "status IN ('pending_verification','active','suspended','locked','anonymized')",
            name="ck_users_status",
        ),
        sa.CheckConstraint(
            "status = 'locked' OR locked_until IS NULL",
            name="ck_users_locked_until",
        ),
    )
    op.create_index("ix_users_email_normalized", "users", ["email_normalized"])


def downgrade() -> None:
    """Remove only the identity table introduced by this revision."""
    op.drop_table("users")

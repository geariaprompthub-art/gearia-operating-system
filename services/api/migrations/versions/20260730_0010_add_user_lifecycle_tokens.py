"""Add P2B lifecycle tokens and workspace operational status.

Revision ID: 20260730_0010
Revises: 20260729_0009
"""

from alembic import op
import sqlalchemy as sa

revision = "20260730_0010"
down_revision = "20260729_0009"
branch_labels = None
depends_on = None


def _token_table(name: str, purpose: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"purpose = '{purpose}'", name=f"ck_{name}_purpose"),
        sa.CheckConstraint("expires_at > created_at", name=f"ck_{name}_expiry"),
        sa.UniqueConstraint("token_hash", name=f"uq_{name}_token_hash"),
    )
    op.create_index(f"ix_{name}_user_purpose_state", name, ["user_id", "purpose", "used_at", "invalidated_at"])
    op.create_index(f"ix_{name}_expires_at", name, ["expires_at"])


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("status", sa.String(length=40), nullable=False, server_default="active"))
    op.create_check_constraint("ck_workspaces_status", "workspaces", "status IN ('active','blocked_by_owner_anonymization')")
    _token_table("email_verification_tokens", "email_verification")
    _token_table("password_reset_tokens", "password_reset")


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_constraint("ck_workspaces_status", "workspaces", type_="check")
    op.drop_column("workspaces", "status")

"""Create the P2A workspace tenancy aggregate and rebuildable visibility projection.

Revision ID: 20260729_0009
Revises: 20260728_0008
"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_0009"
down_revision = "20260728_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add only P2A workspace-private tables; canonical content remains unchanged."""

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("owner_user_id", name="uq_workspaces_owner_user"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_workspaces_name_nonempty"),
    )
    op.create_index("ix_workspaces_owner_user_id", "workspaces", ["owner_user_id"])
    op.create_table(
        "workspace_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("workspace_id", "source_id", name="uq_workspace_sources_identity"),
    )
    op.create_index("ix_workspace_sources_workspace_id", "workspace_sources", ["workspace_id"])
    op.create_index("ix_workspace_sources_source_id", "workspace_sources", ["source_id"])
    op.create_table(
        "workspace_content_visibility",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["content_id"], ["contents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "content_id"),
    )
    op.create_index(
        "ix_workspace_content_visibility_content_id",
        "workspace_content_visibility",
        ["content_id"],
    )


def downgrade() -> None:
    """Remove only P2A tables without touching canonical or shared objects."""

    op.drop_table("workspace_content_visibility")
    op.drop_table("workspace_sources")
    op.drop_table("workspaces")

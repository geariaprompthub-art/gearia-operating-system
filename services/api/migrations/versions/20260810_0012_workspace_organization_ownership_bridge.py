"""Bridge workspace ownership from P2 personal owner to organization ownership."""

from alembic import op
import sqlalchemy as sa


revision = "20260810_0012"
down_revision = "20260805_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    missing_organization = bind.scalar(sa.text("SELECT count(*) FROM workspaces WHERE organization_id IS NULL"))
    if missing_organization:
        raise RuntimeError("cannot enforce workspace organization ownership: organization_id is missing")
    op.alter_column("workspaces", "organization_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("workspaces", "owner_user_id", existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint("uq_workspaces_owner_user", "workspaces", type_="unique")
    op.create_index(
        "uq_workspaces_personal_owner_user_active",
        "workspaces",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    shared_workspaces = bind.scalar(sa.text("SELECT count(*) FROM workspaces WHERE owner_user_id IS NULL"))
    if shared_workspaces:
        raise RuntimeError("cannot downgrade workspace organization ownership while shared workspaces exist")
    duplicate_owners = bind.scalar(
        sa.text("SELECT count(*) FROM (SELECT owner_user_id FROM workspaces GROUP BY owner_user_id HAVING count(*) > 1) AS duplicate_owners")
    )
    if duplicate_owners:
        raise RuntimeError("cannot restore unique workspace owner constraint: duplicate owners exist")
    op.drop_index("uq_workspaces_personal_owner_user_active", table_name="workspaces")
    op.alter_column("workspaces", "owner_user_id", existing_type=sa.Uuid(), nullable=False)
    op.create_unique_constraint("uq_workspaces_owner_user", "workspaces", ["owner_user_id"])
    op.alter_column("workspaces", "organization_id", existing_type=sa.Uuid(), nullable=True)

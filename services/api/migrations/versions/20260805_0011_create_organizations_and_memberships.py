"""Create P3A organizations, memberships, invitations, and workspace backfill.

Revision ID: 20260805_0011
Revises: 20260730_0010
"""

from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa

revision = "20260805_0011"
down_revision = "20260730_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("personal_owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["personal_owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        sa.UniqueConstraint("personal_owner_user_id", name="uq_organizations_personal_owner_user"),
        sa.CheckConstraint("kind IN ('personal','shared')", name="ck_organizations_kind"),
        sa.CheckConstraint("status IN ('active','blocked')", name="ck_organizations_status"),
        sa.CheckConstraint(
            "(kind = 'personal' AND personal_owner_user_id IS NOT NULL) "
            "OR (kind = 'shared' AND personal_owner_user_id IS NULL)",
            name="ck_organizations_personal_owner_shape",
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_organizations_name_nonempty"),
        sa.CheckConstraint("length(trim(slug)) > 0", name="ck_organizations_slug_nonempty"),
    )
    op.create_index("ix_organizations_personal_owner_user_id", "organizations", ["personal_owner_user_id"])

    op.create_table(
        "organization_memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("role IN ('owner','admin','member')", name="ck_organization_memberships_role"),
    )
    op.create_index("ix_organization_memberships_organization_id", "organization_memberships", ["organization_id"])
    op.create_index("ix_organization_memberships_user_id", "organization_memberships", ["user_id"])
    op.create_index(
        "uq_organization_memberships_active_identity",
        "organization_memberships",
        ["organization_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("invited_email_normalized", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_membership_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_membership_id"], ["organization_memberships.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("token_hash", name="uq_organization_invitations_token_hash"),
        sa.CheckConstraint("role IN ('admin','member')", name="ck_organization_invitations_role"),
        sa.CheckConstraint("expires_at > created_at", name="ck_organization_invitations_expiry"),
        sa.CheckConstraint("accepted_at IS NULL OR invalidated_at IS NULL", name="ck_organization_invitations_terminal_state"),
    )
    op.create_index("ix_organization_invitations_organization_id", "organization_invitations", ["organization_id"])
    op.create_index("ix_organization_invitations_created_by_membership_id", "organization_invitations", ["created_by_membership_id"])
    op.create_index(
        "uq_organization_invitations_active_identity",
        "organization_invitations",
        ["organization_id", "invited_email_normalized"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND invalidated_at IS NULL"),
    )

    op.add_column("workspaces", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_workspaces_organization_id_organizations", "workspaces", "organizations", ["organization_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])
    _backfill_personal_organizations()


def _backfill_personal_organizations() -> None:
    """Map every legacy personal workspace to one deterministic personal organization."""

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, owner_user_id, name, status, created_at, updated_at FROM workspaces ORDER BY id")).mappings()
    for workspace in rows:
        owner_id = workspace["owner_user_id"]
        organization_id = uuid5(NAMESPACE_URL, f"gearia:p3a:personal-organization:{owner_id}")
        slug = f"personal-{str(owner_id).replace('-', '')}"
        status = "active" if workspace["status"] == "active" else "blocked"
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, kind, name, slug, status, personal_owner_user_id, blocked_at, created_at, updated_at) "
                "VALUES (:id, 'personal', :name, :slug, :status, :owner_id, NULL, :created_at, :updated_at) "
                "ON CONFLICT (personal_owner_user_id) DO NOTHING"
            ),
            {"id": organization_id, "name": workspace["name"], "slug": slug, "status": status, "owner_id": owner_id, "created_at": workspace["created_at"], "updated_at": workspace["updated_at"]},
        )
        bind.execute(
            sa.text(
                "INSERT INTO organization_memberships (id, organization_id, user_id, role, created_at, updated_at, revoked_at) "
                "VALUES (:id, :organization_id, :user_id, 'owner', :created_at, :updated_at, NULL) ON CONFLICT DO NOTHING"
            ),
            {"id": uuid5(NAMESPACE_URL, f"gearia:p3a:personal-owner-membership:{owner_id}"), "organization_id": organization_id, "user_id": owner_id, "created_at": workspace["created_at"], "updated_at": workspace["updated_at"]},
        )
        bind.execute(
            sa.text("UPDATE workspaces SET organization_id = :organization_id WHERE id = :workspace_id AND organization_id IS NULL"),
            {"organization_id": organization_id, "workspace_id": workspace["id"]},
        )
    remaining = bind.scalar(sa.text("SELECT count(*) FROM workspaces WHERE organization_id IS NULL"))
    if remaining:
        raise RuntimeError("P3A organization backfill left workspace rows without organization_id")


def downgrade() -> None:
    op.drop_index("ix_workspaces_organization_id", table_name="workspaces")
    op.drop_constraint("fk_workspaces_organization_id_organizations", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "organization_id")
    op.drop_index("uq_organization_invitations_active_identity", table_name="organization_invitations")
    op.drop_index("ix_organization_invitations_created_by_membership_id", table_name="organization_invitations")
    op.drop_index("ix_organization_invitations_organization_id", table_name="organization_invitations")
    op.drop_table("organization_invitations")
    op.drop_index("uq_organization_memberships_active_identity", table_name="organization_memberships")
    op.drop_index("ix_organization_memberships_user_id", table_name="organization_memberships")
    op.drop_index("ix_organization_memberships_organization_id", table_name="organization_memberships")
    op.drop_table("organization_memberships")
    op.drop_index("ix_organizations_personal_owner_user_id", table_name="organizations")
    op.drop_table("organizations")

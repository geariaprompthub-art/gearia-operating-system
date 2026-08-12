"""Persisted P3A organizational aggregate and membership records."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OrganizationKind(StrEnum):
    """Organization ownership shapes introduced by P3A."""

    PERSONAL = "personal"
    SHARED = "shared"


class OrganizationStatus(StrEnum):
    """Operational availability of an organization."""

    ACTIVE = "active"
    BLOCKED = "blocked"


class OrganizationMembershipRole(StrEnum):
    """Membership roles intentionally supported by the first P3A persistence layer."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Organization(Base):
    """Aggregate root above workspaces; personal ownership remains explicit."""

    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        UniqueConstraint("personal_owner_user_id", name="uq_organizations_personal_owner_user"),
        CheckConstraint("kind IN ('personal','shared')", name="ck_organizations_kind"),
        CheckConstraint("status IN ('active','blocked')", name="ck_organizations_status"),
        CheckConstraint(
            "(kind = 'personal' AND personal_owner_user_id IS NOT NULL) "
            "OR (kind = 'shared' AND personal_owner_user_id IS NULL)",
            name="ck_organizations_personal_owner_shape",
        ),
        CheckConstraint("length(trim(name)) > 0", name="ck_organizations_name_nonempty"),
        CheckConstraint("length(trim(slug)) > 0", name="ck_organizations_slug_nonempty"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=OrganizationStatus.ACTIVE.value,
        server_default=OrganizationStatus.ACTIVE.value,
    )
    personal_owner_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class OrganizationMembership(Base):
    """A revocable organization membership; historical rows are retained."""

    __tablename__ = "organization_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','member')", name="ck_organization_memberships_role"),
        Index(
            "uq_organization_memberships_active_identity",
            "organization_id",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrganizationInvitation(Base):
    """Opaque, single-use invitation challenge scoped to one organization."""

    __tablename__ = "organization_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_organization_invitations_token_hash"),
        CheckConstraint("role IN ('admin','member')", name="ck_organization_invitations_role"),
        CheckConstraint("expires_at > created_at", name="ck_organization_invitations_expiry"),
        CheckConstraint(
            "accepted_at IS NULL OR invalidated_at IS NULL",
            name="ck_organization_invitations_terminal_state",
        ),
        Index(
            "uq_organization_invitations_active_identity",
            "organization_id",
            "invited_email_normalized",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND invalidated_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invited_email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_membership_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_memberships.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

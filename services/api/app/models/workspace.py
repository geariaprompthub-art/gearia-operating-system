"""Workspace aggregate root for private product data."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkspaceStatus(StrEnum):
    """Operational availability of a personal workspace."""

    ACTIVE = "active"
    BLOCKED_BY_OWNER_ANONYMIZATION = "blocked_by_owner_anonymization"


class Workspace(Base):
    """Personal workspace; the aggregate root for all workspace-private resources."""

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_workspaces_owner_user"),
        CheckConstraint("length(trim(name)) > 0", name="ck_workspaces_name_nonempty"),
        CheckConstraint(
            "status IN ('active','blocked_by_owner_anonymization')",
            name="ck_workspaces_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=WorkspaceStatus.ACTIVE.value,
        server_default=WorkspaceStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

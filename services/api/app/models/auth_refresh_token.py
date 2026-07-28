"""One-time opaque refresh-token records."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuthRefreshToken(Base):
    """Audit rotation lineage while storing only a SHA-256 token hash."""

    __tablename__ = "auth_refresh_tokens"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_auth_refresh_tokens_expiry"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    parent_token_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("auth_refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    replaced_by_token_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("auth_refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reuse_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

"""One-time opaque lifecycle challenges; raw tokens are never persisted."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LifecycleTokenPurpose(StrEnum):
    """Purposes intentionally supported by P2B."""

    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"


class _LifecycleTokenBase:
    """Shared persisted fields for purpose-specific one-time challenges."""

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class EmailVerificationToken(_LifecycleTokenBase, Base):
    """Email-verification token rows only."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        CheckConstraint("purpose = 'email_verification'", name="ck_email_verification_tokens_purpose"),
        CheckConstraint("expires_at > created_at", name="ck_email_verification_tokens_expiry"),
    )


class PasswordResetToken(_LifecycleTokenBase, Base):
    """Password-reset token rows only."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        CheckConstraint("purpose = 'password_reset'", name="ck_password_reset_tokens_purpose"),
        CheckConstraint("expires_at > created_at", name="ck_password_reset_tokens_expiry"),
    )

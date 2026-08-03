"""Opaque P2B lifecycle-token issuance and consumption without commits."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models.lifecycle_tokens import EmailVerificationToken, LifecycleTokenPurpose, PasswordResetToken
from app.models.user import User
from app.repositories.lifecycle_token_repository import EmailVerificationTokenRepository, PasswordResetTokenRepository


class InvalidLifecycleTokenError(ValueError):
    """Sanitized terminal state for invalid, expired, used, or replaced challenges."""


@dataclass(frozen=True)
class IssuedLifecycleToken:
    raw_token: str = field(repr=False)
    token_hash: str = field(repr=False)
    expires_at: datetime


class LifecycleTokenService:
    """Use HMAC-SHA-256 with an injected pepper; callers own the transaction."""

    def __init__(self, database: Session, pepper: str) -> None:
        if not isinstance(pepper, str) or not pepper:
            raise ValueError("lifecycle token pepper is required")
        self._database = database
        self._pepper = pepper.encode("utf-8")
        self._email = EmailVerificationTokenRepository(database)
        self._reset = PasswordResetTokenRepository(database)

    def hash(self, raw_token: object) -> str:
        if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 256:
            raise InvalidLifecycleTokenError("invalid lifecycle token")
        return hmac.new(self._pepper, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue_email_verification(self, user_id: UUID, ttl: timedelta) -> IssuedLifecycleToken:
        return self._issue(self._email, EmailVerificationToken, LifecycleTokenPurpose.EMAIL_VERIFICATION, user_id, ttl)

    def issue_password_reset(self, user_id: UUID, ttl: timedelta) -> IssuedLifecycleToken:
        return self._issue(self._reset, PasswordResetToken, LifecycleTokenPurpose.PASSWORD_RESET, user_id, ttl)

    def consume_email_verification(self, raw_token: object) -> EmailVerificationToken:
        return self._consume(self._email, raw_token)

    def consume_password_reset(self, raw_token: object) -> PasswordResetToken:
        return self._consume(self._reset, raw_token)

    def find_password_reset_user_id(self, raw_token: object) -> UUID | None:
        """Read the owner before locking it, preserving user-then-token lock ordering."""

        row = self._reset.get_by_hash(self.hash(raw_token))
        return None if row is None else row.user_id

    def _issue(self, repository, model, purpose: LifecycleTokenPurpose, user_id: UUID, ttl: timedelta):
        if not isinstance(user_id, UUID) or ttl.total_seconds() <= 0:
            raise ValueError("invalid lifecycle-token issuance")
        # Serialize replacement for one (user, purpose) without a time-dependent index.
        if self._database.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
            raise ValueError("invalid lifecycle-token issuance")
        repository.invalidate_active_for_user(user_id)
        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + ttl
        row = repository.create(model(user_id=user_id, purpose=purpose.value, token_hash=self.hash(raw_token), expires_at=expires_at))
        return IssuedLifecycleToken(raw_token=raw_token, token_hash=row.token_hash, expires_at=expires_at)

    def _consume(self, repository, raw_token: object):
        row = repository.get_by_hash_for_update(self.hash(raw_token))
        now = datetime.now(UTC)
        expires_at = row.expires_at if row is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if row is None or row.used_at is not None or row.invalidated_at is not None or expires_at <= now:
            raise InvalidLifecycleTokenError("invalid lifecycle token")
        repository.mark_used(row)
        return row

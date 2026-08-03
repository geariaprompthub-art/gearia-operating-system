"""Transaction-neutral repositories for P2B one-time lifecycle tokens."""

from datetime import UTC, datetime
from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.lifecycle_tokens import EmailVerificationToken, PasswordResetToken

TokenT = TypeVar("TokenT", EmailVerificationToken, PasswordResetToken)


class _LifecycleTokenRepository(Generic[TokenT]):
    """Lock and mutate caller-owned token rows; never commit or roll back."""

    model: type[TokenT]

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, token: TokenT) -> TokenT:
        self._database.add(token)
        self._database.flush()
        return token

    def get_by_hash(self, token_hash: str) -> TokenT | None:
        return self._database.scalar(select(self.model).where(self.model.token_hash == token_hash))

    def get_by_hash_for_update(self, token_hash: str) -> TokenT | None:
        return self._database.scalar(
            select(self.model).where(self.model.token_hash == token_hash).with_for_update()
        )

    def invalidate_active_for_user(self, user_id: UUID) -> int:
        now = datetime.now(UTC)
        result = self._database.execute(
            update(self.model)
            .where(
                self.model.user_id == user_id,
                self.model.used_at.is_(None),
                self.model.invalidated_at.is_(None),
            )
            .values(invalidated_at=now)
        )
        self._database.flush()
        return int(result.rowcount or 0)

    def mark_used(self, token: TokenT) -> None:
        token.used_at = datetime.now(UTC)
        self._database.flush()

    def delete_for_user(self, user_id: UUID) -> int:
        result = self._database.execute(self.model.__table__.delete().where(self.model.user_id == user_id))
        self._database.flush()
        return int(result.rowcount or 0)


class EmailVerificationTokenRepository(_LifecycleTokenRepository[EmailVerificationToken]):
    model = EmailVerificationToken


class PasswordResetTokenRepository(_LifecycleTokenRepository[PasswordResetToken]):
    model = PasswordResetToken

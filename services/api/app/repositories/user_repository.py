"""Narrow persistence access for internal users."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


class EmailAlreadyExistsError(RuntimeError):
    """Raised only after the database confirms canonical-email duplication."""


class UserRepository:
    """Persist and retrieve users without exposing arbitrary updates."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def get_by_id(self, user_id: UUID) -> User | None:
        return self._database.get(User, user_id)

    def get_by_id_for_update(self, user_id: UUID) -> User | None:
        """Lock one identity before a caller-owned lifecycle transition."""

        return self._database.scalar(select(User).where(User.id == user_id).with_for_update())

    def get_by_normalized_email(self, email_normalized: str) -> User | None:
        return self._database.scalar(select(User).where(User.email_normalized == email_normalized))

    def exists_by_normalized_email(self, email_normalized: str) -> bool:
        return self.get_by_normalized_email(email_normalized) is not None

    def create(self, user: User) -> User:
        """Stage and flush a user; the application boundary owns commit/rollback."""

        self._database.add(user)
        self._database.flush()
        return user

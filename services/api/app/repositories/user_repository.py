"""Narrow persistence access for internal users."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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

    def get_by_normalized_email(self, email_normalized: str) -> User | None:
        return self._database.scalar(select(User).where(User.email_normalized == email_normalized))

    def exists_by_normalized_email(self, email_normalized: str) -> bool:
        return self.get_by_normalized_email(email_normalized) is not None

    def create(self, user: User) -> User:
        try:
            self._database.add(user)
            self._database.commit()
            self._database.refresh(user)
            return user
        except IntegrityError as error:
            self._database.rollback()
            constraint_name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint_name == "uq_users_email_normalized" or (
                constraint_name is None
                and "users.email_normalized" in str(error.orig)
            ):
                raise EmailAlreadyExistsError("email already exists") from error
            raise

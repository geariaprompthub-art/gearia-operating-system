"""Transaction-neutral persistence operations for authentication sessions."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession


class AuthSessionRepository:
    """Mutate the caller-owned SQLAlchemy unit of work without committing."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, session: AuthSession) -> AuthSession:
        self._database.add(session)
        self._database.flush()
        return session

    def get_by_id(self, session_id: UUID) -> AuthSession | None:
        return self._database.get(AuthSession, session_id)

    def get_by_id_for_update(self, session_id: UUID) -> AuthSession | None:
        """Lock one session for a caller-owned state-changing transaction."""

        return self._database.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_id)
            .with_for_update()
        )

    def get_by_refresh_hash_for_update(self, token_hash: str) -> AuthSession | None:
        """Lock the refresh-token owner before the token row is rotated.

        Refresh and logout therefore acquire their shared session lock before
        touching refresh-token rows, avoiding an inverted lock order.
        """

        return self._database.scalar(
            select(AuthSession)
            .join(AuthRefreshToken, AuthRefreshToken.session_id == AuthSession.id)
            .where(AuthRefreshToken.token_hash == token_hash)
            .with_for_update()
        )

    def get_active_by_id(self, session_id: UUID) -> AuthSession | None:
        now = datetime.now(UTC)
        return self._database.scalar(
            select(AuthSession).where(
                AuthSession.id == session_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )

    def revoke(self, session: AuthSession, reason: str) -> None:
        if session.revoked_at is None:
            session.revoked_at = datetime.now(UTC)
            session.revocation_reason = reason
            self._database.flush()

    def update_last_seen(self, session: AuthSession) -> None:
        session.last_seen_at = datetime.now(UTC)
        self._database.flush()

    def list_active_by_user(self, user_id: UUID) -> list[AuthSession]:
        return list(
            self._database.scalars(
                select(AuthSession)
                .where(
                    AuthSession.user_id == user_id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > datetime.now(UTC),
                )
                .order_by(AuthSession.created_at.asc(), AuthSession.id.asc())
            )
        )

    def revoke_all_by_session(self, session_id: UUID, reason: str) -> int:
        result = self._database.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revocation_reason=reason)
        )
        self._database.flush()
        return int(result.rowcount or 0)

    def revoke_all_by_user(self, user_id: UUID, reason: str) -> int:
        result = self._database.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revocation_reason=reason)
        )
        self._database.flush()
        return int(result.rowcount or 0)

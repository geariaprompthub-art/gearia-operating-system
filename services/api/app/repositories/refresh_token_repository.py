"""Transaction-neutral refresh-token persistence operations."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.auth_refresh_token import AuthRefreshToken


class RefreshTokenRepository:
    """Use row locks for rotation; the AuthService owns commit and rollback."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, token: AuthRefreshToken) -> AuthRefreshToken:
        self._database.add(token)
        self._database.flush()
        return token

    def get_by_hash(self, token_hash: str) -> AuthRefreshToken | None:
        return self._database.scalar(
            select(AuthRefreshToken).where(AuthRefreshToken.token_hash == token_hash)
        )

    def get_for_update(self, token_hash: str) -> AuthRefreshToken | None:
        return self._database.scalar(
            select(AuthRefreshToken)
            .where(AuthRefreshToken.token_hash == token_hash)
            .with_for_update()
        )

    def mark_used(self, token: AuthRefreshToken) -> None:
        token.used_at = datetime.now(UTC)
        self._database.flush()

    def mark_replaced(self, token: AuthRefreshToken, successor_id: UUID) -> None:
        token.replaced_by_token_id = successor_id
        self._database.flush()

    def revoke_family(self, family_id: UUID, reason: str = "family_revoked") -> int:
        result = self._database.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.family_id == family_id, AuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        self._database.flush()
        return int(result.rowcount or 0)

    def revoke_session(self, session_id: UUID, reason: str = "session_revoked") -> int:
        result = self._database.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.session_id == session_id, AuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        self._database.flush()
        return int(result.rowcount or 0)

    def delete_for_sessions(self, session_ids: list[UUID]) -> int:
        """Permanently remove refresh-token hashes for an anonymized account."""

        if not session_ids:
            return 0
        result = self._database.execute(
            AuthRefreshToken.__table__.delete().where(AuthRefreshToken.session_id.in_(session_ids))
        )
        self._database.flush()
        return int(result.rowcount or 0)

    def exists_active_successor(self, token_id: UUID) -> bool:
        return self._database.scalar(
            select(AuthRefreshToken.id).where(
                AuthRefreshToken.parent_token_id == token_id,
                AuthRefreshToken.revoked_at.is_(None),
                AuthRefreshToken.expires_at > datetime.now(UTC),
            )
        ) is not None

"""Read-only validation of access tokens against PostgreSQL session state."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.auth_session import AuthSession
from app.models.user import UserStatus
from app.repositories.user_repository import UserRepository
from app.services.jwt_service import InvalidAccessTokenError, JWTService

logger = logging.getLogger(__name__)


class AccessAuthenticationError(RuntimeError):
    """Uniform internal error for every invalid protected access context."""


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Immutable authentication context, deliberately separate from ORM models."""

    user_id: UUID
    session_id: UUID
    token_version: int
    token_jti: UUID
    issued_at: datetime
    expires_at: datetime
    email: str
    user_status: str
    email_verified_at: datetime | None
    created_at: datetime


class AccessTokenAuthenticator:
    """Validate JWT, persisted session and user state without mutating either."""

    def __init__(self, database: Session, jwt_service: JWTService) -> None:
        self._database = database
        self._jwt_service = jwt_service

    def authenticate(self, raw_token: object) -> AuthenticatedPrincipal:
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise AccessAuthenticationError("missing access token")
        try:
            claims = self._jwt_service.validate(raw_token)
        except InvalidAccessTokenError as error:
            logger.info("access_auth_failure category=invalid_token")
            raise AccessAuthenticationError("invalid access token") from error

        session = self._database.get(AuthSession, claims.session_id)
        if (
            session is None
            or session.revoked_at is not None
            or (session.expires_at.replace(tzinfo=UTC) if session.expires_at.tzinfo is None else session.expires_at) <= datetime.now(UTC)
            or session.user_id != claims.user_id
            or session.token_version != claims.token_version
        ):
            logger.info("session_invalid session_id=%s", claims.session_id)
            raise AccessAuthenticationError("invalid authenticated session")
        user = UserRepository(self._database).get_by_id(claims.user_id)
        if user is None or user.status != UserStatus.ACTIVE or user.token_version != claims.token_version:
            logger.info("access_auth_failure category=invalid_user")
            raise AccessAuthenticationError("invalid authenticated user")
        principal = AuthenticatedPrincipal(
            user_id=user.id,
            session_id=session.id,
            token_version=claims.token_version,
            token_jti=claims.jti,
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
            email=user.email,
            user_status=user.status,
            email_verified_at=user.email_verified_at,
            created_at=user.created_at,
        )
        logger.info("access_auth_success user_id=%s session_id=%s", principal.user_id, principal.session_id)
        return principal

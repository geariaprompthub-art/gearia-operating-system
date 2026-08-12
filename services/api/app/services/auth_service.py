"""Dependency-injected transaction boundary for authentication flows."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.user import UserStatus
from app.repositories.user_repository import UserRepository
from app.services.rate_limiter import RateLimitPolicy
from app.services.refresh_token_service import InvalidRefreshTokenError

logger = logging.getLogger(__name__)


class LoginError(RuntimeError):
    """Base class for sanitized domain login failures."""


class InvalidCredentialsError(LoginError):
    """Do not reveal whether an account exists or which credential failed."""


class AccountStatusError(LoginError):
    """Credentials were valid but the account cannot start a session."""


class LoginRateLimitedError(LoginError):
    """Login attempts exceeded the configured rate limit."""


class RefreshError(RuntimeError):
    """Base class for sanitized refresh failures."""


class InvalidRefreshError(RefreshError):
    """Token state is invalid without revealing which state failed."""


class InvalidCsrfError(RefreshError):
    """CSRF cookie/header pair is absent, mismatched, or unbound to the session."""


class RefreshReuseDetectedError(RefreshError):
    """A consumed refresh token was reused and its session was revoked."""


class RefreshRateLimitedError(RefreshError):
    """Refresh attempts exceeded the configured rate limit."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("refresh rate limited")
        self.retry_after = retry_after


class LogoutError(RuntimeError):
    """Base class for sanitized logout failures."""


class InvalidLogoutSessionError(LogoutError):
    """The validated principal no longer maps to a usable persisted session."""


class InvalidLogoutCsrfError(LogoutError):
    """The state-changing logout request lacks a valid bound CSRF pair."""


@dataclass(frozen=True)
class LoginResult:
    """Domain-only result; raw token material is intentionally repr-safe."""

    user_id: UUID
    email: str
    session_id: UUID
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class RefreshResult:
    """Domain refresh result; no persistence model or token material is public."""

    user_id: UUID
    session_id: UUID
    access_token: str = field(repr=False)
    access_expires_at: datetime
    refresh_token: str = field(repr=False)
    refresh_expires_at: datetime
    csrf_token: str = field(repr=False)
    csrf_expires_at: datetime


@dataclass(frozen=True)
class LogoutResult:
    """Minimal domain result without ORM instances or credential material."""

    user_id: UUID
    session_id: UUID
    revoked_at: datetime
    already_revoked: bool


class AuthService:
    """Own one commit/rollback boundary while remaining independent of FastAPI."""

    def __init__(self, database: Session, identity_service: object, jwt_service: object, refresh_token_service: object, cookie_policy: object, csrf_service: object, session_repository: object, refresh_token_repository: object, rate_limiter: object, login_rate_limit_policy: RateLimitPolicy | None = None, refresh_ttl_seconds: int = 2_592_000, refresh_rate_limit_policy: RateLimitPolicy | None = None, access_ttl_seconds: int = 600) -> None:
        self._database = database
        self._identity_service = identity_service
        self._jwt_service = jwt_service
        self._refresh_token_service = refresh_token_service
        self._cookie_policy = cookie_policy
        self._csrf_service = csrf_service
        self._session_repository = session_repository
        self._refresh_token_repository = refresh_token_repository
        self._rate_limiter = rate_limiter
        self._login_rate_limit_policy = login_rate_limit_policy or RateLimitPolicy("auth:login", 5, 60)
        self._refresh_ttl = refresh_ttl_seconds
        self._refresh_rate_limit_policy = refresh_rate_limit_policy or RateLimitPolicy("auth:refresh", 20, 60)
        self._access_ttl = access_ttl_seconds

    @property
    def cookie_policy(self) -> object:
        """Expose the central policy to the HTTP boundary without exposing tokens."""

        return self._cookie_policy

    def login(self, email: object, password: object) -> LoginResult:
        """Create an authenticated session after generic credential verification."""

        identifier = email.strip().casefold() if isinstance(email, str) else "invalid"
        decision = self._rate_limiter.consume(self._login_rate_limit_policy, identifier)
        if not decision.allowed:
            raise LoginRateLimitedError("login rate limited")

        try:
            credentials = self._identity_service.verify_credentials(email, password)
            if credentials.status == "invalid_credentials":
                raise InvalidCredentialsError("invalid credentials")
            if credentials.status != "valid":
                raise AccountStatusError(credentials.status)
            if credentials.user is None:
                raise InvalidCredentialsError("invalid credentials")

            csrf_token, csrf_hash = self._csrf_service.issue()
            session = self._session_repository.create(
                AuthSession(
                    user_id=credentials.user.id,
                    token_version=credentials.user.token_version,
                    csrf_secret_hash=csrf_hash,
                    expires_at=datetime.now(UTC) + timedelta(seconds=self._refresh_ttl),
                )
            )
            refresh = self._refresh_token_service.issue()
            self._refresh_token_repository.create(
                AuthRefreshToken(
                    id=refresh.token_id,
                    session_id=session.id,
                    family_id=uuid4(),
                    token_hash=refresh.token_hash,
                    expires_at=datetime.now(UTC) + timedelta(seconds=self._refresh_ttl),
                )
            )
            access_token = self._jwt_service.issue(
                credentials.user.id, session.id, credentials.user.token_version
            )
            self._database.commit()
            return LoginResult(
                user_id=credentials.user.id,
                email=credentials.user.email,
                session_id=session.id,
                access_token=access_token,
                refresh_token=refresh.raw_token,
                csrf_token=csrf_token,
            )
        except LoginError:
            self._database.rollback()
            raise
        except Exception:
            self._database.rollback()
            raise

    def refresh(
        self,
        raw_token: object,
        csrf_cookie: object,
        csrf_header: object,
        client_ip: object,
    ) -> RefreshResult:
        """Rotate one opaque refresh token atomically and revoke replayed sessions."""

        self._consume_refresh_rate_limit("ip", client_ip)
        try:
            token_id = self._refresh_token_service.parse(raw_token)
        except InvalidRefreshTokenError as error:
            raise InvalidRefreshError("invalid refresh token") from error
        self._consume_refresh_rate_limit("token", str(token_id))

        try:
            token_hash = self._refresh_token_service.hash(raw_token)
            session = self._session_repository.get_by_refresh_hash_for_update(token_hash)
            token = self._refresh_token_repository.get_for_update(token_hash)
            if (
                session is None
                or token is None
                or token.session_id != session.id
                or not self._refresh_token_service.matches(raw_token, token.token_hash)
            ):
                raise InvalidRefreshError("invalid refresh token")
            if self._expired(session.expires_at) or session.revoked_at is not None:
                raise InvalidRefreshError("invalid refresh token")
            if token.used_at is not None or token.replaced_by_token_id is not None:
                self._revoke_compromised_session(session, token)
                self._database.commit()
                raise RefreshReuseDetectedError("refresh token reuse detected")
            if not self._csrf_valid(csrf_cookie, csrf_header, session.csrf_secret_hash):
                raise InvalidCsrfError("invalid csrf token")
            if self._expired(token.expires_at) or token.revoked_at is not None:
                raise InvalidRefreshError("invalid refresh token")

            user = UserRepository(self._database).get_by_id(session.user_id)
            if user is None or user.status != UserStatus.ACTIVE:
                raise InvalidRefreshError("invalid refresh token")

            csrf_token, csrf_hash = self._csrf_service.issue()
            successor = self._refresh_token_service.issue()
            now = datetime.now(UTC)
            successor_expires_at = now + timedelta(seconds=self._refresh_ttl)
            successor_record = self._refresh_token_repository.create(
                AuthRefreshToken(
                    id=successor.token_id,
                    session_id=session.id,
                    family_id=token.family_id,
                    token_hash=successor.token_hash,
                    parent_token_id=token.id,
                    expires_at=successor_expires_at,
                )
            )
            self._refresh_token_repository.mark_used(token)
            self._refresh_token_repository.mark_replaced(token, successor_record.id)
            session.csrf_secret_hash = csrf_hash
            self._session_repository.update_last_seen(session)
            access_token = self._jwt_service.issue(user.id, session.id, session.token_version)
            self._database.commit()
            logger.info("refresh_success user_id=%s session_id=%s", user.id, session.id)
            return RefreshResult(
                user_id=user.id,
                session_id=session.id,
                access_token=access_token,
                access_expires_at=now + timedelta(seconds=self._access_ttl),
                refresh_token=successor.raw_token,
                refresh_expires_at=successor_expires_at,
                csrf_token=csrf_token,
                csrf_expires_at=successor_expires_at,
            )
        except RefreshReuseDetectedError:
            raise
        except RefreshError:
            self._database.rollback()
            raise
        except Exception:
            self._database.rollback()
            raise

    def logout(
        self,
        principal: object,
        csrf_cookie: object,
        csrf_header: object,
    ) -> LogoutResult:
        """Revoke exactly one authenticated session and all of its refresh tokens.

        The HTTP boundary authenticates the access cookie first.  This service
        locks the authoritative session again before checking CSRF and changing
        state, making concurrent logout attempts safe and atomic.
        """

        session_id = getattr(principal, "session_id", None)
        user_id = getattr(principal, "user_id", None)
        token_version = getattr(principal, "token_version", None)
        if not isinstance(session_id, UUID) or not isinstance(user_id, UUID) or not isinstance(token_version, int):
            raise InvalidLogoutSessionError("invalid logout principal")

        try:
            session = self._session_repository.get_by_id_for_update(session_id)
            if (
                session is None
                or session.user_id != user_id
                or session.token_version != token_version
                or self._expired(session.expires_at)
            ):
                raise InvalidLogoutSessionError("invalid logout session")
            if not self._csrf_valid(csrf_cookie, csrf_header, session.csrf_secret_hash):
                logger.info("logout_csrf_rejected user_id=%s session_id=%s", user_id, session_id)
                raise InvalidLogoutCsrfError("invalid csrf token")
            if session.revoked_at is not None:
                self._database.commit()
                logger.info("logout_already_revoked user_id=%s session_id=%s", user_id, session_id)
                return LogoutResult(user_id, session_id, session.revoked_at, True)

            self._session_repository.revoke(session, "user_logout")
            self._refresh_token_repository.revoke_session(session.id, "user_logout")
            self._database.commit()
            assert session.revoked_at is not None
            logger.info("logout_success user_id=%s session_id=%s", user_id, session_id)
            return LogoutResult(user_id, session_id, session.revoked_at, False)
        except LogoutError:
            self._database.rollback()
            raise
        except Exception:
            self._database.rollback()
            logger.warning("logout_failure user_id=%s session_id=%s", user_id, session_id)
            raise

    def _consume_refresh_rate_limit(self, namespace_suffix: str, identifier: object) -> None:
        policy = RateLimitPolicy(
            f"{self._refresh_rate_limit_policy.namespace}:{namespace_suffix}",
            self._refresh_rate_limit_policy.limit,
            self._refresh_rate_limit_policy.window_seconds,
            self._refresh_rate_limit_policy.failure_mode,
        )
        decision = self._rate_limiter.consume(policy, identifier if isinstance(identifier, str) and identifier else "unknown")
        if not decision.allowed:
            logger.warning("refresh_rate_limited category=%s", namespace_suffix)
            raise RefreshRateLimitedError(decision.retry_after)

    def _csrf_valid(self, csrf_cookie: object, csrf_header: object, expected_hash: str) -> bool:
        return self._csrf_service.valid_pair(csrf_cookie, csrf_header, expected_hash)

    @staticmethod
    def _expired(value: datetime) -> bool:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value <= datetime.now(UTC)

    def _revoke_compromised_session(self, session: AuthSession, token: AuthRefreshToken) -> None:
        """Persist the mandatory session-wide replay response in the active transaction."""

        now = datetime.now(UTC)
        token.reuse_detected_at = now
        self._session_repository.revoke(session, "refresh_reuse_detected")
        self._refresh_token_repository.revoke_session(session.id, "refresh_reuse_detected")
        logger.warning("refresh_reuse_detected session_id=%s", session.id)

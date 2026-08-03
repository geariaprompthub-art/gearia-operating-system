"""Public password-reset orchestration without HTTP, sessions, or providers."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.auth_session import AuthSession
from app.models.user import User, UserStatus
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.email_delivery import EmailDeliveryAdapter
from app.services.email_normalization import normalize_email
from app.services.lifecycle_token_service import InvalidLifecycleTokenError, LifecycleTokenService
from app.services.password_hasher import PasswordHashingService
from app.services.rate_limiter import RateLimitPolicy


class PasswordResetRateLimitedError(RuntimeError):
    """A password-reset request was rejected before sensitive work."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("password reset rate limited")
        self.retry_after = retry_after


class PasswordResetUnavailableError(RuntimeError):
    """Sanitized operational failure during password reset."""


@dataclass(frozen=True)
class PasswordResetRequestResult:
    """Internal result that never exposes account existence publicly."""

    issued: bool
    delivery_failed: bool


@dataclass(frozen=True)
class PasswordResetConfirmResult:
    """Internal result that preserves public idempotence."""

    completed_now: bool


class PasswordResetApplicationService:
    """Own reset request and confirmation commits while reusing P2B primitives."""

    def __init__(
        self,
        database: Session,
        lifecycle_token_service: LifecycleTokenService,
        password_hasher: PasswordHashingService,
        session_repository: AuthSessionRepository,
        refresh_token_repository: RefreshTokenRepository,
        rate_limiter: object,
        request_ip_policy: RateLimitPolicy,
        request_email_policy: RateLimitPolicy,
        confirm_ip_policy: RateLimitPolicy,
        confirm_token_policy: RateLimitPolicy,
        email_delivery_adapter: EmailDeliveryAdapter,
        reset_ttl: timedelta = timedelta(hours=1),
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._database = database
        self._lifecycle_tokens = lifecycle_token_service
        self._password_hasher = password_hasher
        self._sessions = session_repository
        self._refresh_tokens = refresh_token_repository
        self._rate_limiter = rate_limiter
        self._request_ip_policy = request_ip_policy
        self._request_email_policy = request_email_policy
        self._confirm_ip_policy = confirm_ip_policy
        self._confirm_token_policy = confirm_token_policy
        self._email_delivery_adapter = email_delivery_adapter
        self._reset_ttl = reset_ttl
        self._structured_logger = structured_logger

    def request(self, email: str, client_ip: str, correlation_id: str | None) -> PasswordResetRequestResult:
        """Issue only for active users while returning a uniform acknowledgement."""

        normalized = normalize_email(email)
        self._consume_limit(self._request_ip_policy, client_ip or "unknown")
        self._consume_limit(self._request_email_policy, normalized.normalized)
        try:
            user = self._database.scalar(
                select(User)
                .where(User.email_normalized == normalized.normalized)
                .with_for_update()
            )
            if user is None or user.status != UserStatus.ACTIVE:
                return PasswordResetRequestResult(False, False)
            issued = self._lifecycle_tokens.issue_password_reset(user.id, self._reset_ttl)
            self._database.commit()
        except Exception as error:
            self._database.rollback()
            self._log_failure(error)
            raise PasswordResetUnavailableError("password reset unavailable") from error

        try:
            self._email_delivery_adapter.send_password_reset(
                normalized.email, issued.raw_token, correlation_id
            )
        except Exception as error:
            self._log_failure(error)
            return PasswordResetRequestResult(True, True)
        return PasswordResetRequestResult(True, False)

    def confirm(self, raw_token: str, password: str, client_ip: str) -> PasswordResetConfirmResult:
        """Consume once, replace credentials, and revoke all existing authentication."""

        self._consume_limit(self._confirm_ip_policy, client_ip or "unknown")
        self._consume_limit(self._confirm_token_policy, raw_token)
        try:
            user_id = self._lifecycle_tokens.find_password_reset_user_id(raw_token)
        except InvalidLifecycleTokenError:
            return PasswordResetConfirmResult(False)
        except Exception as error:
            self._log_failure(error)
            raise PasswordResetUnavailableError("password reset unavailable") from error
        if user_id is None:
            return PasswordResetConfirmResult(False)

        try:
            # Account anonymization and reset confirmation both lock user before token.
            user = self._database.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or user.status != UserStatus.ACTIVE:
                return PasswordResetConfirmResult(False)
            try:
                self._lifecycle_tokens.consume_password_reset(raw_token)
            except InvalidLifecycleTokenError:
                self._database.rollback()
                return PasswordResetConfirmResult(False)
            user.password_hash = self._password_hasher.hash(password)
            user.token_version += 1
            session_ids = list(
                self._database.scalars(select(AuthSession.id).where(AuthSession.user_id == user.id))
            )
            self._sessions.revoke_all_by_user(user.id, "password_reset")
            for session_id in session_ids:
                self._refresh_tokens.revoke_session(session_id, "password_reset")
            self._database.flush()
            self._database.commit()
            return PasswordResetConfirmResult(True)
        except PasswordResetUnavailableError:
            self._database.rollback()
            raise
        except Exception as error:
            self._database.rollback()
            self._log_failure(error)
            raise PasswordResetUnavailableError("password reset unavailable") from error

    def _consume_limit(self, policy: RateLimitPolicy, identifier: str) -> None:
        decision = self._rate_limiter.consume(policy, identifier)
        if not decision.allowed:
            raise PasswordResetRateLimitedError(decision.retry_after)

    def _log_failure(self, error: Exception) -> None:
        if self._structured_logger is not None:
            self._structured_logger.error(
                LogEvent.PASSWORD_RESET_FAILED,
                "Password reset failed",
                error_type=type(error).__name__,
            )

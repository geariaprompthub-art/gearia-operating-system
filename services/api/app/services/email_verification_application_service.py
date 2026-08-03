"""Public-email-verification orchestration without HTTP or session concerns."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.user import User, UserStatus
from app.services.lifecycle_token_service import InvalidLifecycleTokenError, LifecycleTokenService
from app.services.rate_limiter import RateLimitPolicy


class EmailVerificationRateLimitedError(RuntimeError):
    """Verification was rejected before any token-consumption work."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("email verification rate limited")
        self.retry_after = retry_after


class EmailVerificationUnavailableError(RuntimeError):
    """A sanitized operational failure while confirming a verification token."""


@dataclass(frozen=True)
class EmailVerificationResult:
    """Internal result; only the stable acknowledgement is exposed publicly."""

    verified_now: bool


class EmailVerificationApplicationService:
    """Consume one verification challenge and activate only its pending user."""

    def __init__(
        self,
        database: Session,
        lifecycle_token_service: LifecycleTokenService,
        rate_limiter: object,
        ip_rate_limit_policy: RateLimitPolicy,
        token_rate_limit_policy: RateLimitPolicy,
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._database = database
        self._lifecycle_token_service = lifecycle_token_service
        self._rate_limiter = rate_limiter
        self._ip_rate_limit_policy = ip_rate_limit_policy
        self._token_rate_limit_policy = token_rate_limit_policy
        self._structured_logger = structured_logger

    def confirm(self, raw_token: str, client_ip: str) -> EmailVerificationResult:
        """Commit activation once; invalid or previously consumed tokens are idempotent."""

        self._consume_limit(self._ip_rate_limit_policy, client_ip or "unknown")
        self._consume_limit(self._token_rate_limit_policy, raw_token)
        try:
            token = self._lifecycle_token_service.consume_email_verification(raw_token)
        except InvalidLifecycleTokenError:
            return EmailVerificationResult(verified_now=False)
        except Exception as error:
            self._log_failure(error)
            raise EmailVerificationUnavailableError("email verification unavailable") from error

        try:
            user = self._database.scalar(select(User).where(User.id == token.user_id).with_for_update())
            if user is None or user.status != UserStatus.PENDING_VERIFICATION:
                raise EmailVerificationUnavailableError("email verification unavailable")
            user.status = UserStatus.ACTIVE
            user.email_verified_at = datetime.now(UTC)
            self._database.flush()
            self._database.commit()
            return EmailVerificationResult(verified_now=True)
        except EmailVerificationUnavailableError:
            self._database.rollback()
            raise
        except Exception as error:
            self._database.rollback()
            self._log_failure(error)
            raise EmailVerificationUnavailableError("email verification unavailable") from error

    def _consume_limit(self, policy: RateLimitPolicy, identifier: str) -> None:
        decision = self._rate_limiter.consume(policy, identifier)
        if not decision.allowed:
            raise EmailVerificationRateLimitedError(decision.retry_after)

    def _log_failure(self, error: Exception) -> None:
        if self._structured_logger is not None:
            self._structured_logger.error(
                LogEvent.EMAIL_VERIFICATION_FAILED,
                "Email verification failed",
                error_type=type(error).__name__,
            )

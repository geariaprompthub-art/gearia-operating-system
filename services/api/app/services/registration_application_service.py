"""HTTP-adjacent registration orchestration without FastAPI dependencies."""

from dataclasses import dataclass

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.services.email_delivery import EmailDeliveryAdapter
from app.services.email_normalization import normalize_email
from app.services.rate_limiter import RateLimitPolicy
from app.services.registration_service import RegistrationError, RegistrationService


class RegistrationRateLimitedError(RuntimeError):
    """The public registration policy rejected a request before expensive work."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("registration rate limited")
        self.retry_after = retry_after


class RegistrationUnavailableError(RuntimeError):
    """A sanitized operational registration failure."""


@dataclass(frozen=True)
class RegistrationSubmissionResult:
    """Public-flow outcome that intentionally contains no account state."""

    delivery_attempted: bool
    delivery_failed: bool


class RegistrationApplicationService:
    """Rate-limit and post-commit delivery around the atomic registration service."""

    def __init__(
        self,
        registration_service: RegistrationService,
        rate_limiter: object,
        ip_rate_limit_policy: RateLimitPolicy,
        email_rate_limit_policy: RateLimitPolicy,
        email_delivery_adapter: EmailDeliveryAdapter,
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._registration_service = registration_service
        self._rate_limiter = rate_limiter
        self._ip_rate_limit_policy = ip_rate_limit_policy
        self._email_rate_limit_policy = email_rate_limit_policy
        self._email_delivery_adapter = email_delivery_adapter
        self._structured_logger = structured_logger

    def submit(
        self,
        email: str,
        password: str,
        client_ip: str,
        correlation_id: str | None,
    ) -> RegistrationSubmissionResult:
        """Commit registration first; delivery failures never reopen that transaction."""

        normalized = normalize_email(email)
        self._consume(self._ip_rate_limit_policy, client_ip or "unknown")
        self._consume(self._email_rate_limit_policy, normalized.normalized)
        try:
            result = self._registration_service.register(normalized.email, password)
        except RegistrationError:
            # Existing non-pending identities are indistinguishable from a new request.
            return RegistrationSubmissionResult(False, False)
        except Exception as error:
            raise RegistrationUnavailableError("registration unavailable") from error

        try:
            self._email_delivery_adapter.send_email_verification(
                normalized.email,
                result.raw_verification_token,
                correlation_id,
            )
        except Exception as error:
            if self._structured_logger is not None:
                self._structured_logger.error(
                    LogEvent.REGISTRATION_DELIVERY_FAILED,
                    "Registration verification delivery failed",
                    error_type=type(error).__name__,
                )
            return RegistrationSubmissionResult(True, True)
        return RegistrationSubmissionResult(True, False)

    def _consume(self, policy: RateLimitPolicy, identifier: str) -> None:
        decision = self._rate_limiter.consume(policy, identifier)
        if not decision.allowed:
            raise RegistrationRateLimitedError(decision.retry_after)

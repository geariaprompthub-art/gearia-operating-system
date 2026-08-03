"""Irreversible authenticated account anonymization for P2B."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.user import UserStatus
from app.models.workspace import WorkspaceStatus
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.lifecycle_token_repository import (
    EmailVerificationTokenRepository,
    PasswordResetTokenRepository,
)
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.csrf_service import CsrfService
from app.services.rate_limiter import RateLimitPolicy


class AccountAnonymizationCsrfError(RuntimeError):
    """The browser did not prove possession of the session-bound CSRF secret."""


class AccountAnonymizationRateLimitedError(RuntimeError):
    """Account deletion is rate limited before any state transition."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("account anonymization rate limited")
        self.retry_after = retry_after


class AccountAnonymizationUnavailableError(RuntimeError):
    """Sanitized terminal error for failed anonymization transactions."""


@dataclass(frozen=True)
class AccountAnonymizationResult:
    """No PII or persistence objects cross the application boundary."""

    anonymized_now: bool


class AccountAnonymizationApplicationService:
    """Own the sole commit/rollback boundary for irreversible account closure."""

    def __init__(
        self,
        database: Session,
        csrf_service: CsrfService,
        session_repository: AuthSessionRepository,
        refresh_token_repository: RefreshTokenRepository,
        rate_limiter: object,
        ip_rate_limit_policy: RateLimitPolicy,
        user_rate_limit_policy: RateLimitPolicy,
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._database = database
        self._csrf = csrf_service
        self._sessions = session_repository
        self._refresh_tokens = refresh_token_repository
        self._rate_limiter = rate_limiter
        self._ip_policy = ip_rate_limit_policy
        self._user_policy = user_rate_limit_policy
        self._users = UserRepository(database)
        self._workspaces = WorkspaceRepository(database)
        self._email_tokens = EmailVerificationTokenRepository(database)
        self._reset_tokens = PasswordResetTokenRepository(database)
        self._logger = structured_logger

    def anonymize_account(
        self,
        principal: AuthenticatedPrincipal,
        csrf_cookie: object,
        csrf_header: object,
        client_ip: str,
    ) -> AccountAnonymizationResult:
        """Revoke credentials, erase user PII, and retain a blocked workspace atomically."""

        self._consume_limit(self._ip_policy, client_ip or "unknown")
        self._consume_limit(self._user_policy, str(principal.user_id))
        session = self._sessions.get_by_id(principal.session_id)
        if (
            session is None
            or session.user_id != principal.user_id
            or session.revoked_at is not None
            or not self._csrf.valid(csrf_cookie, session.csrf_secret_hash)
            or not self._csrf.valid(csrf_header, session.csrf_secret_hash)
        ):
            raise AccountAnonymizationCsrfError("invalid account deletion csrf")

        try:
            # Global order: user row, personal workspace, then credential rows.
            user = self._users.get_by_id_for_update(principal.user_id)
            if user is None:
                raise AccountAnonymizationUnavailableError("account unavailable")
            workspace = self._workspaces.get_by_owner_user_id_for_update(user.id)
            if workspace is None:
                raise AccountAnonymizationUnavailableError("account unavailable")
            if user.status == UserStatus.ANONYMIZED:
                return AccountAnonymizationResult(anonymized_now=False)

            session_ids = self._sessions.revoke_and_anonymize_all_by_user(
                user.id, "owner_anonymized"
            )
            for session_id in session_ids:
                self._refresh_tokens.revoke_session(session_id, "owner_anonymized")
            self._refresh_tokens.delete_for_sessions(session_ids)
            self._email_tokens.delete_for_user(user.id)
            self._reset_tokens.delete_for_user(user.id)

            synthetic_email = f"deleted+{user.id}@invalid.local"
            user.email = synthetic_email
            user.email_normalized = synthetic_email
            user.password_hash = None
            user.email_verified_at = None
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = None
            user.token_version += 1
            user.status = UserStatus.ANONYMIZED
            workspace.status = WorkspaceStatus.BLOCKED_BY_OWNER_ANONYMIZATION
            self._database.flush()
            self._database.commit()
        except AccountAnonymizationUnavailableError:
            self._database.rollback()
            raise
        except Exception as error:
            self._database.rollback()
            self._log_failure(error)
            raise AccountAnonymizationUnavailableError("account anonymization unavailable") from error

        if self._logger is not None:
            self._logger.info(
                LogEvent.ACCOUNT_ANONYMIZED,
                "Account anonymized",
                user_id=str(user.id),
                workspace_id=str(workspace.id),
                status=UserStatus.ANONYMIZED,
            )
        return AccountAnonymizationResult(anonymized_now=True)

    def _consume_limit(self, policy: RateLimitPolicy, identifier: str) -> None:
        decision = self._rate_limiter.consume(policy, identifier)
        if not decision.allowed:
            raise AccountAnonymizationRateLimitedError(decision.retry_after)

    def _log_failure(self, error: Exception) -> None:
        if self._logger is not None:
            self._logger.error(
                LogEvent.ACCOUNT_ANONYMIZATION_FAILED,
                "Account anonymization failed",
                error_type=type(error).__name__,
            )

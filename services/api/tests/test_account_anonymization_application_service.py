"""P2B account anonymization transaction and PostgreSQL locking contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, SessionLocal as PostgresSessionLocal
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.lifecycle_tokens import EmailVerificationToken, PasswordResetToken
from app.models.user import User, UserStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.account_anonymization_application_service import (
    AccountAnonymizationApplicationService,
    AccountAnonymizationCsrfError,
)
from app.services.auth_service import AuthService, InvalidRefreshError, RefreshReuseDetectedError
from app.services.cookie_policy import CookiePolicy
from app.services.csrf_service import CsrfService
from app.services.identity_service import IdentityService
from app.services.jwt_service import JWTService
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.password_hasher import PasswordHashingService
from app.services.password_reset_application_service import PasswordResetApplicationService
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.rate_limiter import RateLimitDecision, RateLimitPolicy
from app.services.refresh_token_service import RefreshTokenService
from app.services.workspace_context import WorkspaceContext
from app.services.workspace_service import WorkspaceBlockedError, WorkspaceService


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)


class AllowingLimiter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def consume(self, policy: RateLimitPolicy, identifier: str) -> RateLimitDecision:
        self.calls.append(policy.namespace)
        return RateLimitDecision(True, 1, 0, 0)


def _service(database: Session, limiter: AllowingLimiter | None = None) -> AccountAnonymizationApplicationService:
    return AccountAnonymizationApplicationService(
        database,
        CsrfService(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        limiter or AllowingLimiter(),
        RateLimitPolicy("auth:account-anonymization:ip", 3, 60),
        RateLimitPolicy("auth:account-anonymization:user", 3, 60),
    )


def _state(database: Session, *, prefix: str = "anonymize") -> tuple[User, Workspace, AuthSession, str]:
    email = f"{prefix}-{uuid4().hex}@example.com"
    user = User(
        email=email,
        email_normalized=email,
        password_hash="argon2id-test-hash",
        status=UserStatus.ACTIVE,
        email_verified_at=datetime.now(UTC),
        failed_login_count=2,
        last_login_at=datetime.now(UTC),
    )
    database.add(user); database.flush()
    workspace = Workspace(owner_user_id=user.id, name="Personal workspace")
    csrf, csrf_hash = CsrfService().issue()
    session = AuthSession(
        user_id=user.id,
        token_version=user.token_version,
        csrf_secret_hash=csrf_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        ip_address="127.0.0.1",
        user_agent="test-agent",
    )
    database.add_all([workspace, session]); database.flush()
    database.add(AuthRefreshToken(session_id=session.id, family_id=uuid4(), token_hash=uuid4().hex * 2, expires_at=datetime.now(UTC) + timedelta(hours=1)))
    LifecycleTokenService(database, "test-pepper").issue_email_verification(user.id, timedelta(minutes=5))
    LifecycleTokenService(database, "test-pepper").issue_password_reset(user.id, timedelta(minutes=5))
    database.commit(); database.refresh(user); database.refresh(workspace); database.refresh(session)
    return user, workspace, session, csrf


def _principal(user: User, session: AuthSession) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(user.id, session.id, user.token_version, uuid4(), now, now + timedelta(minutes=10), user.email, user.status, user.email_verified_at, user.created_at)


class _JwtStub:
    def issue(self, *_: object) -> str:
        return "test-access-token"


def _auth_service(database: Session) -> AuthService:
    return AuthService(
        database,
        IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)),
        _JwtStub(),
        RefreshTokenService(),
        CookiePolicy(False, "lax", None, 300, 3600),
        CsrfService(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        AllowingLimiter(),
        RateLimitPolicy("auth:login", 5, 60),
        3600,
        RateLimitPolicy("auth:refresh", 20, 60),
        300,
    )


@pytest.fixture(autouse=True)
def tables() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def test_anonymization_erases_identity_revokes_credentials_and_blocks_workspace() -> None:
    with SessionLocal() as database:
        user, workspace, session, csrf = _state(database)
        original_email = user.email
        limiter = AllowingLimiter()
        result = _service(database, limiter).anonymize_account(_principal(user, session), csrf, csrf, "127.0.0.1")
        database.expire_all()
        retained_user = database.get(User, user.id)
        retained_workspace = database.get(Workspace, workspace.id)
        retained_session = database.get(AuthSession, session.id)

        assert result.anonymized_now is True
        assert retained_user is not None and retained_user.status == UserStatus.ANONYMIZED
        assert retained_user.email == f"deleted+{user.id}@invalid.local"
        assert retained_user.email_normalized == retained_user.email
        assert retained_user.email != original_email and retained_user.password_hash is None
        assert retained_user.email_verified_at is None and retained_user.failed_login_count == 0
        assert retained_user.last_login_at is None and retained_user.token_version == 2
        assert retained_workspace is not None and retained_workspace.status == WorkspaceStatus.BLOCKED_BY_OWNER_ANONYMIZATION
        assert retained_session is not None and retained_session.revoked_at is not None
        assert retained_session.ip_address is None and retained_session.user_agent is None
        assert database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == session.id)).all() == []
        assert database.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)).all() == []
        assert database.scalars(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)).all() == []
        assert limiter.calls == ["auth:account-anonymization:ip", "auth:account-anonymization:user"]
        with pytest.raises(WorkspaceBlockedError):
            WorkspaceService(database).get_current(WorkspaceContext(workspace.id, user.id))


def test_invalid_csrf_leaves_everything_unchanged() -> None:
    with SessionLocal() as database:
        user, workspace, session, csrf = _state(database)
        with pytest.raises(AccountAnonymizationCsrfError):
            _service(database).anonymize_account(_principal(user, session), csrf, "mismatch", "127.0.0.1")
        assert database.get(User, user.id).status == UserStatus.ACTIVE  # type: ignore[union-attr]
        assert database.get(Workspace, workspace.id).status == WorkspaceStatus.ACTIVE  # type: ignore[union-attr]
        assert database.get(AuthSession, session.id).revoked_at is None  # type: ignore[union-attr]


def test_rollback_preserves_user_workspace_and_credentials() -> None:
    with SessionLocal() as database:
        user, workspace, session, csrf = _state(database)
        service = _service(database)
        def broken(_: list[object]) -> int:
            raise RuntimeError("controlled deletion failure")
        service._refresh_tokens.delete_for_sessions = broken  # type: ignore[method-assign]
        with pytest.raises(Exception):
            service.anonymize_account(_principal(user, session), csrf, csrf, "127.0.0.1")
        assert database.get(User, user.id).status == UserStatus.ACTIVE  # type: ignore[union-attr]
        assert database.get(Workspace, workspace.id).status == WorkspaceStatus.ACTIVE  # type: ignore[union-attr]
        assert database.get(AuthSession, session.id).revoked_at is None  # type: ignore[union-attr]


def test_original_email_is_available_to_a_new_unrelated_user_after_anonymization() -> None:
    with SessionLocal() as database:
        user, workspace, session, csrf = _state(database)
        original_email = user.email
        _service(database).anonymize_account(_principal(user, session), csrf, csrf, "127.0.0.1")
        replacement = User(email=original_email, email_normalized=original_email, password_hash="new-hash", status=UserStatus.PENDING_VERIFICATION)
        database.add(replacement); database.flush()
        replacement_workspace = Workspace(owner_user_id=replacement.id, name="Personal workspace")
        database.add(replacement_workspace); database.commit()
        assert replacement.id != user.id and replacement_workspace.owner_user_id == replacement.id
        assert workspace.owner_user_id == user.id


def test_postgresql_concurrent_delete_keeps_one_consistent_anonymized_state() -> None:
    marker = f"p2b-anonymize-{uuid4().hex}"
    with PostgresSessionLocal() as database:
        user, workspace, session, csrf = _state(database, prefix=marker)
        user_id, workspace_id, session_id = user.id, workspace.id, session.id
        principal = _principal(user, session)
    barrier = Barrier(2)

    def delete_account() -> str:
        with PostgresSessionLocal() as database:
            barrier.wait(timeout=10)
            try:
                result = _service(database).anonymize_account(principal, csrf, csrf, "127.0.0.1")
                return "changed" if result.anonymized_now else "already"
            except AccountAnonymizationCsrfError:
                return "invalidated"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: delete_account(), range(2)))
        with PostgresSessionLocal() as verification:
            final_user = verification.get(User, user_id)
            final_workspace = verification.get(Workspace, workspace_id)
            assert "changed" in outcomes
            assert final_user is not None and final_user.status == UserStatus.ANONYMIZED
            assert final_workspace is not None and final_workspace.status == WorkspaceStatus.BLOCKED_BY_OWNER_ANONYMIZATION
    finally:
        with PostgresSessionLocal() as cleanup:
            cleanup.execute(delete(AuthRefreshToken).where(AuthRefreshToken.session_id == session_id))
            cleanup.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            cleanup.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
            cleanup.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
            cleanup.execute(delete(Workspace).where(Workspace.id == workspace_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_postgresql_delete_and_password_reset_cannot_restore_anonymized_credentials() -> None:
    marker = f"p2b-anonymize-reset-{uuid4().hex}"
    with PostgresSessionLocal() as database:
        user, workspace, session, csrf = _state(database, prefix=marker)
        issued = LifecycleTokenService(database, "test-pepper").issue_password_reset(
            user.id, timedelta(minutes=5)
        )
        database.commit()
        principal = _principal(user, session)
        user_id, workspace_id, session_id = user.id, workspace.id, session.id
    barrier = Barrier(2)

    def delete_account() -> str:
        with PostgresSessionLocal() as database:
            barrier.wait(timeout=10)
            return "deleted" if _service(database).anonymize_account(principal, csrf, csrf, "127.0.0.1").anonymized_now else "already"

    def reset_password() -> bool:
        with PostgresSessionLocal() as database:
            limiter = AllowingLimiter()
            service = PasswordResetApplicationService(
                database, LifecycleTokenService(database, "test-pepper"),
                PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1),
                AuthSessionRepository(database), RefreshTokenRepository(database), limiter,
                RateLimitPolicy("reset-ip", 10, 60), RateLimitPolicy("reset-token", 10, 60),
                RateLimitPolicy("reset-confirm-ip", 10, 60), RateLimitPolicy("reset-confirm-token", 10, 60),
                FakeEmailDeliveryAdapter(),
            )
            barrier.wait(timeout=10)
            return service.confirm(issued.raw_token, "new valid password", "127.0.0.1").completed_now

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda operation: operation(), (delete_account, reset_password)))
        with PostgresSessionLocal() as verification:
            final_user = verification.get(User, user_id)
            final_workspace = verification.get(Workspace, workspace_id)
            assert "deleted" in outcomes
            assert final_user is not None and final_user.status == UserStatus.ANONYMIZED
            assert final_user.password_hash is None
            assert final_workspace is not None and final_workspace.status == WorkspaceStatus.BLOCKED_BY_OWNER_ANONYMIZATION
            assert verification.scalars(select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)).all() == []
    finally:
        with PostgresSessionLocal() as cleanup:
            cleanup.execute(delete(AuthRefreshToken).where(AuthRefreshToken.session_id == session_id))
            cleanup.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            cleanup.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
            cleanup.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
            cleanup.execute(delete(Workspace).where(Workspace.id == workspace_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_postgresql_delete_and_refresh_cannot_resurrect_anonymized_account() -> None:
    """A concurrent refresh may not restore credentials after account deletion."""

    marker = f"p2b-anonymize-refresh-{uuid4().hex}"
    with PostgresSessionLocal() as database:
        user, workspace, session, csrf = _state(database, prefix=marker)
        issued = RefreshTokenService().issue()
        database.add(
            AuthRefreshToken(
                id=issued.token_id,
                session_id=session.id,
                family_id=uuid4(),
                token_hash=issued.token_hash,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        database.commit()
        principal = _principal(user, session)
        user_id, workspace_id, session_id = user.id, workspace.id, session.id
    barrier = Barrier(2)

    def delete_account() -> str:
        with PostgresSessionLocal() as database:
            barrier.wait(timeout=10)
            return "deleted" if _service(database).anonymize_account(
                principal, csrf, csrf, "127.0.0.1"
            ).anonymized_now else "already"

    def refresh() -> str:
        with PostgresSessionLocal() as database:
            barrier.wait(timeout=10)
            try:
                _auth_service(database).refresh(issued.raw_token, csrf, csrf, "127.0.0.1")
                return "refreshed"
            except (InvalidRefreshError, RefreshReuseDetectedError):
                return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda operation: operation(), (delete_account, refresh)))
        with PostgresSessionLocal() as verification:
            final_user = verification.get(User, user_id)
            final_workspace = verification.get(Workspace, workspace_id)
            final_session = verification.get(AuthSession, session_id)
            active_refresh = list(
                verification.scalars(
                    select(AuthRefreshToken).where(
                        AuthRefreshToken.session_id == session_id,
                        AuthRefreshToken.revoked_at.is_(None),
                    )
                )
            )
            assert "deleted" in outcomes
            assert final_user is not None and final_user.status == UserStatus.ANONYMIZED
            assert final_user.password_hash is None and final_user.token_version == 2
            assert final_workspace is not None and final_workspace.status == WorkspaceStatus.BLOCKED_BY_OWNER_ANONYMIZATION
            assert final_session is not None and final_session.revoked_at is not None
            assert active_refresh == []
    finally:
        with PostgresSessionLocal() as cleanup:
            cleanup.execute(delete(AuthRefreshToken).where(AuthRefreshToken.session_id == session_id))
            cleanup.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            cleanup.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
            cleanup.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
            cleanup.execute(delete(Workspace).where(Workspace.id == workspace_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()

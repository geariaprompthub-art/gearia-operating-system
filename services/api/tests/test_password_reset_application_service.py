"""Transactional P2B password-reset contracts, including PostgreSQL concurrency."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
import logging
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.db import SessionLocal as PostgresSessionLocal
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.lifecycle_tokens import EmailVerificationToken, PasswordResetToken
from app.models.user import User, UserStatus
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.password_hasher import PasswordHashingService
from app.services.password_reset_application_service import (
    PasswordResetApplicationService,
    PasswordResetRateLimitedError,
    PasswordResetUnavailableError,
)
from app.services.rate_limiter import RateLimitDecision, RateLimitPolicy


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)


class ControlledRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[RateLimitPolicy, str]] = []

    def consume(self, policy: RateLimitPolicy, identifier: str) -> RateLimitDecision:
        self.calls.append((policy, identifier))
        return RateLimitDecision(self.allowed, 1 if self.allowed else 0, 100, 31 if not self.allowed else 0)


def _hasher() -> PasswordHashingService:
    return PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)


def _service(
    database: Session,
    *,
    adapter: FakeEmailDeliveryAdapter | None = None,
    limiter: ControlledRateLimiter | None = None,
    logger: SafeStructuredLogger | None = None,
) -> PasswordResetApplicationService:
    return PasswordResetApplicationService(
        database,
        LifecycleTokenService(database, "test-pepper"),
        _hasher(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        limiter or ControlledRateLimiter(),
        RateLimitPolicy("auth:password-reset:request:ip", 5, 60),
        RateLimitPolicy("auth:password-reset:request:email", 5, 60),
        RateLimitPolicy("auth:password-reset:confirm:ip", 10, 60),
        RateLimitPolicy("auth:password-reset:confirm:token", 10, 60),
        adapter or FakeEmailDeliveryAdapter(capture_deliveries=True),
        structured_logger=logger,
    )


def _user(database: Session, status: str = UserStatus.ACTIVE) -> User:
    email = f"reset-{uuid4().hex}@example.com"
    user = User(
        email=email,
        email_normalized=email,
        password_hash=_hasher().hash("old valid password"),
        status=status,
    )
    database.add(user)
    database.commit()
    return user


def _issue_reset(database: Session, user: User) -> str:
    issued = LifecycleTokenService(database, "test-pepper").issue_password_reset(
        user.id, timedelta(minutes=10)
    )
    database.commit()
    return issued.raw_token


@pytest.fixture(autouse=True)
def reset_tables() -> None:
    for table in (
        AuthRefreshToken.__table__,
        AuthSession.__table__,
        EmailVerificationToken.__table__,
        PasswordResetToken.__table__,
        User.__table__,
    ):
        table.drop(engine, checkfirst=True)
    User.__table__.create(engine)
    AuthSession.__table__.create(engine)
    AuthRefreshToken.__table__.create(engine)
    EmailVerificationToken.__table__.create(engine)
    PasswordResetToken.__table__.create(engine)


def test_request_issues_hash_only_token_after_commit_for_active_user() -> None:
    with SessionLocal() as database:
        user = _user(database)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        limiter = ControlledRateLimiter()
        result = _service(database, adapter=adapter, limiter=limiter).request(user.email, "127.0.0.1", "request-id")
        token = database.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))

        assert result.issued is True and result.delivery_failed is False
        assert token is not None and token.token_hash != adapter.deliveries[0].raw_token
        assert adapter.call_count == 1 and adapter.deliveries[0].template == "password_reset"
        assert [policy.namespace for policy, _ in limiter.calls] == [
            "auth:password-reset:request:ip",
            "auth:password-reset:request:email",
        ]


@pytest.mark.parametrize(
    "status",
    [UserStatus.PENDING_VERIFICATION, UserStatus.LOCKED, UserStatus.SUSPENDED, UserStatus.ANONYMIZED],
)
def test_request_is_uniform_and_issues_nothing_for_non_active_or_unknown_users(status: str) -> None:
    with SessionLocal() as database:
        user = _user(database, status)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        service = _service(database, adapter=adapter)
        blocked = service.request(user.email, "127.0.0.1", None)
        unknown = service.request("unknown@example.com", "127.0.0.1", None)

        assert blocked.issued is unknown.issued is False
        assert adapter.call_count == 0
        assert database.scalar(select(PasswordResetToken.id)) is None


def test_request_adapter_failure_preserves_committed_token_and_never_rolls_back() -> None:
    with SessionLocal() as database:
        user = _user(database)
        adapter = FakeEmailDeliveryAdapter(fail=True)
        result = _service(database, adapter=adapter).request(user.email, "127.0.0.1", None)

        assert result.issued is True and result.delivery_failed is True
        assert adapter.call_count == 1
        assert database.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)) is not None


def test_request_calls_delivery_only_after_the_token_transaction_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    with SessionLocal() as database:
        user = _user(database)
        events: list[str] = []
        original_commit = database.commit

        class OrderedAdapter(FakeEmailDeliveryAdapter):
            def send_password_reset(self, recipient: str, raw_token: str, correlation_id: str | None) -> None:
                events.append("delivery")
                super().send_password_reset(recipient, raw_token, correlation_id)

        def commit() -> None:
            events.append("commit")
            original_commit()

        monkeypatch.setattr(database, "commit", commit)
        result = _service(database, adapter=OrderedAdapter(capture_deliveries=True)).request(
            user.email, "127.0.0.1", None
        )

        assert result.issued is True
        assert events == ["commit", "delivery"]


def test_confirm_replaces_argon_hash_increments_version_and_revokes_existing_authentication() -> None:
    with SessionLocal() as database:
        user = _user(database)
        raw_token = _issue_reset(database, user)
        session = AuthSession(
            user_id=user.id,
            token_version=user.token_version,
            csrf_secret_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        database.add(session)
        database.flush()
        refresh = AuthRefreshToken(
            session_id=session.id,
            family_id=uuid4(),
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        database.add(refresh)
        database.commit()
        old_hash, old_version = user.password_hash, user.token_version
        limiter = ControlledRateLimiter()

        result = _service(database, limiter=limiter).confirm(raw_token, "new valid password", "127.0.0.1")
        updated = database.get(User, user.id)
        token = database.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
        revoked_session = database.get(AuthSession, session.id)
        revoked_refresh = database.get(AuthRefreshToken, refresh.id)

        assert result.completed_now is True
        assert updated is not None and updated.password_hash != old_hash
        assert _hasher().verify("new valid password", updated.password_hash)
        assert not _hasher().verify("old valid password", updated.password_hash)
        assert updated.token_version == old_version + 1
        assert token is not None and token.used_at is not None
        assert revoked_session is not None and revoked_session.revoked_at is not None
        assert revoked_refresh is not None and revoked_refresh.revoked_at is not None
        assert [policy.namespace for policy, _ in limiter.calls] == [
            "auth:password-reset:confirm:ip",
            "auth:password-reset:confirm:token",
        ]
        assert len(database.new) == 0


def test_confirm_invalid_expired_used_and_invalidated_tokens_are_idempotent() -> None:
    with SessionLocal() as database:
        user = _user(database)
        raw_token = _issue_reset(database, user)
        service = _service(database)
        assert service.confirm(raw_token, "new valid password", "127.0.0.1").completed_now is True
        password_hash, token_version = database.get(User, user.id).password_hash, database.get(User, user.id).token_version
        assert service.confirm(raw_token, "different valid password", "127.0.0.1").completed_now is False
        assert service.confirm("invalid-token", "different valid password", "127.0.0.1").completed_now is False
        assert database.get(User, user.id).password_hash == password_hash
        assert database.get(User, user.id).token_version == token_version

    with SessionLocal() as database:
        user = _user(database)
        expired = _issue_reset(database, user)
        row = database.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == LifecycleTokenService(database, "test-pepper").hash(expired)
            )
        )
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(minutes=2)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        database.commit()
        assert _service(database).confirm(expired, "new valid password", "127.0.0.1").completed_now is False

    with SessionLocal() as database:
        user = _user(database)
        invalidated = _issue_reset(database, user)
        _issue_reset(database, user)
        assert _service(database).confirm(invalidated, "new valid password", "127.0.0.1").completed_now is False


def test_rate_limit_precedes_request_and_confirm_mutations() -> None:
    with SessionLocal() as database:
        user = _user(database)
        denied = ControlledRateLimiter(allowed=False)
        with pytest.raises(PasswordResetRateLimitedError):
            _service(database, limiter=denied).request(user.email, "127.0.0.1", None)
        assert len(denied.calls) == 1
        assert database.scalar(select(PasswordResetToken.id)) is None


def test_unexpected_failure_logs_only_sanitized_metadata() -> None:
    class FailingLifecycleTokenService:
        def consume_password_reset(self, raw_token: str) -> object:
            raise RuntimeError("controlled backend failure")

    stream = StringIO()
    logger = logging.getLogger(f"password-reset-{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    logger.addHandler(handler)
    service = PasswordResetApplicationService(
        object(),  # type: ignore[arg-type]
        FailingLifecycleTokenService(),  # type: ignore[arg-type]
        _hasher(),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        ControlledRateLimiter(),
        RateLimitPolicy("auth:password-reset:request:ip", 5, 60),
        RateLimitPolicy("auth:password-reset:request:email", 5, 60),
        RateLimitPolicy("auth:password-reset:confirm:ip", 10, 60),
        RateLimitPolicy("auth:password-reset:confirm:token", 10, 60),
        FakeEmailDeliveryAdapter(),
        structured_logger=SafeStructuredLogger(logger),
    )
    try:
        with pytest.raises(PasswordResetUnavailableError):
            service.confirm("private-opaque-token", "new valid password", "127.0.0.1")
    finally:
        logger.handlers.clear()

    rendered = stream.getvalue()
    assert "password_reset_failed" in rendered
    assert "private-opaque-token" not in rendered
    assert "controlled backend failure" not in rendered


def test_postgresql_concurrent_confirmation_consumes_once_and_revokes_once() -> None:
    email = f"reset-concurrent-{uuid4().hex}@example.com"
    user_id = None
    with PostgresSessionLocal() as database:
        user = User(email=email, email_normalized=email, password_hash=_hasher().hash("old valid password"), status=UserStatus.ACTIVE)
        database.add(user)
        database.commit()
        raw_token = LifecycleTokenService(database, "test-pepper").issue_password_reset(user.id, timedelta(minutes=5)).raw_token
        database.commit()
        user_id = user.id
    barrier = Barrier(2)
    try:
        def confirm() -> bool:
            with PostgresSessionLocal() as database:
                barrier.wait(timeout=10)
                return PasswordResetApplicationService(
                    database,
                    LifecycleTokenService(database, "test-pepper"),
                    _hasher(),
                    AuthSessionRepository(database),
                    RefreshTokenRepository(database),
                    ControlledRateLimiter(),
                    RateLimitPolicy("auth:password-reset:request:ip", 5, 60),
                    RateLimitPolicy("auth:password-reset:request:email", 5, 60),
                    RateLimitPolicy("auth:password-reset:confirm:ip", 10, 60),
                    RateLimitPolicy("auth:password-reset:confirm:token", 10, 60),
                    FakeEmailDeliveryAdapter(),
                ).confirm(raw_token, "new valid password", "127.0.0.1").completed_now

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: confirm(), range(2), timeout=15))
        with PostgresSessionLocal() as database:
            user = database.get(User, user_id)
            token = database.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
            assert sorted(outcomes) == [False, True]
            assert user is not None and user.token_version == 2
            assert token is not None and token.used_at is not None
    finally:
        if user_id is not None:
            with PostgresSessionLocal() as database:
                database.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
                database.execute(delete(User).where(User.id == user_id))
                database.commit()

"""Transactional P2B verification behavior, including real PostgreSQL locking."""

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

from app.db import SessionLocal as PostgresSessionLocal
from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.lifecycle_tokens import EmailVerificationToken, PasswordResetToken
from app.models.user import User, UserStatus
from app.services.email_verification_application_service import (
    EmailVerificationApplicationService,
    EmailVerificationRateLimitedError,
    EmailVerificationUnavailableError,
)
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.rate_limiter import RateLimitDecision, RateLimitPolicy


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)


class ControlledRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[RateLimitPolicy, str]] = []

    def consume(self, policy: RateLimitPolicy, identifier: str) -> RateLimitDecision:
        self.calls.append((policy, identifier))
        return RateLimitDecision(self.allowed, 1 if self.allowed else 0, 100, 30 if not self.allowed else 0)


def _service(database: Session, limiter: ControlledRateLimiter | None = None) -> EmailVerificationApplicationService:
    return EmailVerificationApplicationService(
        database,
        LifecycleTokenService(database, "test-pepper"),
        limiter or ControlledRateLimiter(),
        RateLimitPolicy("auth:verify:ip", 10, 60),
        RateLimitPolicy("auth:verify:token", 10, 60),
    )


def _issue(database: Session) -> tuple[User, str]:
    email = f"verify-{uuid4().hex}@example.com"
    user = User(email=email, email_normalized=email, password_hash="hash")
    database.add(user)
    database.commit()
    issued = LifecycleTokenService(database, "test-pepper").issue_email_verification(user.id, timedelta(minutes=10))
    database.commit()
    return user, issued.raw_token


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


def test_confirm_activates_pending_user_marks_token_used_and_creates_no_session() -> None:
    with SessionLocal() as database:
        user, raw_token = _issue(database)
        limiter = ControlledRateLimiter()
        result = _service(database, limiter).confirm(raw_token, "127.0.0.1")
        token = database.scalar(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id))
        activated = database.get(User, user.id)

        assert result.verified_now is True
        assert activated is not None and activated.status == UserStatus.ACTIVE
        assert activated.email_verified_at is not None
        assert token is not None and token.used_at is not None
        assert len(database.new) == 0
        assert database.scalar(select(AuthSession.id)) is None
        assert database.scalar(select(AuthRefreshToken.id)) is None
        assert [policy.namespace for policy, _ in limiter.calls] == [
            "auth:verify:ip",
            "auth:verify:token",
        ]


def test_confirm_invalid_expired_used_and_invalidated_tokens_are_idempotent_without_mutation() -> None:
    with SessionLocal() as database:
        user, raw_token = _issue(database)
        service = _service(database)
        assert service.confirm(raw_token, "127.0.0.1").verified_now is True
        verified_at = database.get(User, user.id).email_verified_at
        assert service.confirm(raw_token, "127.0.0.1").verified_now is False
        assert database.get(User, user.id).email_verified_at == verified_at
        assert service.confirm("not-a-valid-token", "127.0.0.1").verified_now is False

    with SessionLocal() as database:
        _, expired_raw = _issue(database)
        row = database.scalar(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash
                == LifecycleTokenService(database, "test-pepper").hash(expired_raw)
            )
        )
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(minutes=2)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        database.commit()
        assert _service(database).confirm(expired_raw, "127.0.0.1").verified_now is False

    with SessionLocal() as database:
        user, invalidated_raw = _issue(database)
        LifecycleTokenService(database, "test-pepper").issue_email_verification(
            user.id, timedelta(minutes=10)
        )
        database.commit()
        assert _service(database).confirm(invalidated_raw, "127.0.0.1").verified_now is False


def test_confirm_rate_limits_before_token_consumption() -> None:
    with SessionLocal() as database:
        _, raw_token = _issue(database)
        limiter = ControlledRateLimiter(allowed=False)
        with pytest.raises(EmailVerificationRateLimitedError):
            _service(database, limiter).confirm(raw_token, "127.0.0.1")
        assert len(limiter.calls) == 1
        assert database.scalar(select(EmailVerificationToken.used_at)) is None


def test_confirm_rejects_non_pending_user_and_rolls_back_token_consumption() -> None:
    with SessionLocal() as database:
        user, raw_token = _issue(database)
        user.status = UserStatus.SUSPENDED
        database.commit()
        with pytest.raises(EmailVerificationUnavailableError):
            _service(database).confirm(raw_token, "127.0.0.1")
        token = database.scalar(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id))
        assert token is not None and token.used_at is None
        assert database.get(User, user.id).status == UserStatus.SUSPENDED
        assert database.is_active


def test_unexpected_consumption_failure_is_sanitized_without_logging_token() -> None:
    class FailingLifecycleTokenService:
        def consume_email_verification(self, raw_token: str) -> object:
            raise RuntimeError("controlled token backend failure")

    stream = StringIO()
    logger = logging.getLogger(f"email-verification-{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    logger.addHandler(handler)
    service = EmailVerificationApplicationService(
        object(),  # type: ignore[arg-type]
        FailingLifecycleTokenService(),  # type: ignore[arg-type]
        ControlledRateLimiter(),
        RateLimitPolicy("auth:verify:ip", 10, 60),
        RateLimitPolicy("auth:verify:token", 10, 60),
        SafeStructuredLogger(logger),
    )
    try:
        with pytest.raises(EmailVerificationUnavailableError):
            service.confirm("private-opaque-token", "127.0.0.1")
    finally:
        logger.handlers.clear()

    rendered = stream.getvalue()
    assert "email_verification_failed" in rendered
    assert "private-opaque-token" not in rendered
    assert "controlled token backend failure" not in rendered


def test_postgresql_concurrent_confirmation_consumes_once_without_deadlock() -> None:
    email = f"verify-concurrent-{uuid4().hex}@example.com"
    user_id = None
    with PostgresSessionLocal() as database:
        user = User(email=email, email_normalized=email, password_hash="hash")
        database.add(user)
        database.commit()
        issued = LifecycleTokenService(database, "test-pepper").issue_email_verification(user.id, timedelta(minutes=5))
        database.commit()
        user_id = user.id
    barrier = Barrier(2)
    try:
        def confirm() -> bool:
            with PostgresSessionLocal() as database:
                barrier.wait(timeout=10)
                return EmailVerificationApplicationService(
                    database,
                    LifecycleTokenService(database, "test-pepper"),
                    ControlledRateLimiter(),
                    RateLimitPolicy("auth:verify:ip", 10, 60),
                    RateLimitPolicy("auth:verify:token", 10, 60),
                ).confirm(issued.raw_token, "127.0.0.1").verified_now

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: confirm(), range(2), timeout=15))
        with PostgresSessionLocal() as database:
            user = database.get(User, user_id)
            token = database.scalar(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
            assert sorted(outcomes) == [False, True]
            assert user is not None and user.status == UserStatus.ACTIVE and user.email_verified_at is not None
            assert token is not None and token.used_at is not None
    finally:
        with PostgresSessionLocal() as database:
            if user_id is not None:
                database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
                database.execute(delete(User).where(User.id == user_id))
                database.commit()

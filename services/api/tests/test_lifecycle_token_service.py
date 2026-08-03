"""Characterization for P2B opaque lifecycle-token infrastructure."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.lifecycle_tokens import LifecycleTokenPurpose
from app.models.user import User
from app.repositories.lifecycle_token_repository import EmailVerificationTokenRepository
from app.services.lifecycle_token_service import InvalidLifecycleTokenError, LifecycleTokenService


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def reset_tables() -> None:
    from app.models.lifecycle_tokens import EmailVerificationToken, PasswordResetToken

    for table in (EmailVerificationToken.__table__, PasswordResetToken.__table__, User.__table__):
        table.drop(engine, checkfirst=True)
    User.__table__.create(engine)
    EmailVerificationToken.__table__.create(engine)
    PasswordResetToken.__table__.create(engine)


@pytest.fixture
def database() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_issue_persists_only_hash_and_replaces_active_challenge(database) -> None:
    user = User(email="token@example.com", email_normalized="token@example.com", password_hash="hash")
    database.add(user); database.commit()
    service = LifecycleTokenService(database, "test-pepper")
    first = service.issue_email_verification(user.id, timedelta(minutes=10))
    second = service.issue_email_verification(user.id, timedelta(minutes=10))
    database.commit()
    repository = EmailVerificationTokenRepository(database)
    first_row = repository.get_by_hash(first.token_hash)
    second_row = repository.get_by_hash(second.token_hash)
    assert first.raw_token != second.raw_token
    assert first.raw_token not in repr(second_row)
    assert first_row.invalidated_at is not None
    assert second_row.invalidated_at is None


def test_consume_is_single_use_and_neutral_until_caller_commits(database) -> None:
    user = User(email="consume@example.com", email_normalized="consume@example.com", password_hash="hash")
    database.add(user); database.commit()
    service = LifecycleTokenService(database, "test-pepper")
    issued = service.issue_email_verification(user.id, timedelta(minutes=10)); database.commit()
    row = service.consume_email_verification(issued.raw_token)
    assert row.used_at is not None
    database.rollback()
    assert EmailVerificationTokenRepository(database).get_by_hash(issued.token_hash).used_at is None
    service.consume_email_verification(issued.raw_token); database.commit()
    with pytest.raises(InvalidLifecycleTokenError):
        service.consume_email_verification(issued.raw_token)


def test_expired_or_wrong_purpose_tokens_are_rejected(database) -> None:
    user = User(email="expired@example.com", email_normalized="expired@example.com", password_hash="hash")
    database.add(user); database.commit()
    service = LifecycleTokenService(database, "test-pepper")
    issued = service.issue_email_verification(user.id, timedelta(minutes=10)); database.commit()
    row = EmailVerificationTokenRepository(database).get_by_hash(issued.token_hash)
    row.created_at = datetime.now(UTC) - timedelta(minutes=2)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1); database.commit()
    with pytest.raises(InvalidLifecycleTokenError):
        service.consume_email_verification(issued.raw_token)

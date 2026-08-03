"""PostgreSQL integration coverage for P1A user persistence invariants."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import User, UserStatus
from app.repositories.user_repository import EmailAlreadyExistsError, UserRepository
from app.services.password_hasher import PasswordHashingService


EMAIL_PREFIX = "p1a-postgres-"


def fast_hasher() -> PasswordHashingService:
    """Avoid production-cost hashing while preserving Argon2id behavior."""
    return PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)


@pytest.fixture(autouse=True)
def remove_test_users() -> None:
    """Remove only test-identified users before and after each integration test."""
    def cleanup() -> None:
        with SessionLocal() as session:
            session.execute(delete(User).where(User.email_normalized.like(f"{EMAIL_PREFIX}%")))
            session.commit()

    cleanup()
    try:
        yield
    finally:
        cleanup()


def make_user(email_suffix: str, **overrides: object) -> User:
    """Build a valid user whose email can be safely cleaned up by the fixture."""
    email = f"{EMAIL_PREFIX}{email_suffix}@example.com"
    values: dict[str, object] = {
        "email": email,
        "email_normalized": email,
        "password_hash": fast_hasher().hash("valid password"),
    }
    values.update(overrides)
    return User(**values)


def test_repository_persists_defaults_and_retrieves_by_id_and_email() -> None:
    with SessionLocal() as session:
        repository = UserRepository(session)
        created = repository.create(make_user("repository"))

        assert created.status == UserStatus.PENDING_VERIFICATION
        assert created.token_version == 1
        assert created.failed_login_count == 0
        assert created.created_at is not None
        assert created.updated_at is not None
        assert repository.get_by_id(created.id) is not None
        assert repository.get_by_normalized_email(created.email_normalized) is not None
        assert repository.exists_by_normalized_email(created.email_normalized)


def test_transaction_boundary_rolls_back_after_duplicate_email_and_remains_usable() -> None:
    with SessionLocal() as session:
        repository = UserRepository(session)
        created = repository.create(make_user("duplicate"))
        session.commit()

        with pytest.raises(IntegrityError):
            repository.create(
                make_user(
                    "duplicate",
                    email=f"{EMAIL_PREFIX}DUPLICATE@example.com",
                )
            )

        session.rollback()
        assert repository.get_by_id(created.id) is not None
        assert session.is_active


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "invalid"},
        {"token_version": 0},
        {"failed_login_count": -1},
        {"status": UserStatus.ACTIVE, "locked_until": datetime.now(UTC)},
        {
            "status": UserStatus.PENDING_VERIFICATION,
            "locked_until": datetime.now(UTC),
        },
        {"status": UserStatus.SUSPENDED, "locked_until": datetime.now(UTC)},
        {"status": UserStatus.ANONYMIZED, "locked_until": datetime.now(UTC)},
    ],
)
def test_postgresql_rejects_user_check_constraint_violations(
    overrides: dict[str, object],
) -> None:
    with SessionLocal() as session:
        session.add(make_user(str(uuid4()), **overrides))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.is_active


@pytest.mark.parametrize(
    "locked_until",
    [
        None,
        datetime.now(UTC) + timedelta(minutes=5),
        datetime.now(UTC) - timedelta(minutes=5),
    ],
)
def test_postgresql_allows_locked_user_with_null_future_or_past_expiry(
    locked_until: datetime | None,
) -> None:
    with SessionLocal() as session:
        user = make_user(
            str(uuid4()), status=UserStatus.LOCKED, locked_until=locked_until
        )
        session.add(user)
        session.commit()
        assert session.get(User, user.id) is not None


def test_concurrent_canonical_email_writes_are_serialized_by_postgresql() -> None:
    """Use two independent sessions synchronized immediately before commit."""
    canonical_email = f"{EMAIL_PREFIX}concurrent@example.com"
    barrier = Barrier(2)

    def attempt(display_email: str) -> str:
        session: Session = SessionLocal()
        try:
            session.add(
                User(
                    email=display_email,
                    email_normalized=canonical_email,
                    password_hash=fast_hasher().hash("valid password"),
                )
            )
            barrier.wait(timeout=10)
            try:
                session.commit()
                return "created"
            except IntegrityError:
                session.rollback()
                return "duplicate"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                attempt,
                [f"{EMAIL_PREFIX}CONCURRENT@example.com", canonical_email],
            )
        )

    with SessionLocal() as verification_session:
        persisted = verification_session.query(User).filter_by(
            email_normalized=canonical_email
        ).count()

    assert sorted(outcomes) == ["created", "duplicate"]
    assert persisted == 1

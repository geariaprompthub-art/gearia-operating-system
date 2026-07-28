"""Unit coverage for the internal P1A identity core.

These tests deliberately use SQLite only for unit behavior. PostgreSQL-specific
constraints and concurrent writes are covered in a dedicated integration suite.
"""

from datetime import UTC, datetime, timedelta

import pytest
from argon2 import Type
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.user import User, UserStatus
from app.repositories.user_repository import EmailAlreadyExistsError, UserRepository
from app.services.email_normalization import InvalidEmailError, normalize_email
from app.services.identity_service import IdentityService
from app.services.password_hasher import InvalidPasswordError, PasswordHashingService


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def reset_users_table() -> None:
    """Keep each unit test independent without creating unrelated tables."""
    User.__table__.drop(engine, checkfirst=True)
    User.__table__.create(engine)


@pytest.fixture
def database() -> Session:
    """Provide a short-lived unit-test database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def fast_hasher() -> PasswordHashingService:
    """Use intentionally cheap parameters for deterministic unit tests."""
    return PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)


@pytest.mark.parametrize(
    ("raw", "email", "normalized"),
    [
        ("  Test+Alias@ExAmple.COM  ", "Test+Alias@ExAmple.COM", "test+alias@example.com"),
        ("first.last@example.com", "first.last@example.com", "first.last@example.com"),
        ("\u00dcser@EXAMPLE.com", "\u00dcser@EXAMPLE.com", "\u00fcser@example.com"),
    ],
)
def test_normalize_email_preserves_identity_without_provider_rewriting(
    raw: str,
    email: str,
    normalized: str,
) -> None:
    result = normalize_email(raw)

    assert result.email == email
    assert result.normalized == normalized


@pytest.mark.parametrize(
    "value",
    ["", "missing-at", "name@localhost", "name@", "@example.com", "x" * 321, None, 7],
)
def test_normalize_email_rejects_invalid_values(value: object) -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(value)


def test_canonical_email_duplication_is_detected(database: Session) -> None:
    service = IdentityService(database, fast_hasher())
    service.create_local_user("User@Example.com", "valid password")

    with pytest.raises(EmailAlreadyExistsError):
        service.create_local_user(" user@example.COM ", "another password")


@pytest.mark.parametrize("length", [11, 129])
def test_password_policy_rejects_outside_boundaries(length: int) -> None:
    with pytest.raises(InvalidPasswordError):
        fast_hasher().hash("x" * length)


@pytest.mark.parametrize("password", ["x" * 12, "x" * 128, " pass word \u00e4 "])
def test_password_policy_accepts_boundaries_and_preserves_spaces(password: str) -> None:
    assert fast_hasher().validate(password) == password


@pytest.mark.parametrize("value", ["", None, 123])
def test_password_policy_rejects_empty_and_non_strings(value: object) -> None:
    with pytest.raises(InvalidPasswordError):
        fast_hasher().hash(value)


def test_argon2id_hashing_verification_and_rehash_contract() -> None:
    hasher = fast_hasher()
    password = "valid password"
    first = hasher.hash(password)
    second = hasher.hash(password)

    assert hasher._hasher.type is Type.ID
    assert hasher._hasher.time_cost == 1
    assert hasher._hasher.memory_cost == 1024
    assert first != second
    assert password not in first
    assert hasher.verify(password, first)
    assert not hasher.verify("incorrect password", first)
    assert not hasher.verify(password, "$argon2id$malformed")
    assert not hasher.needs_rehash(first)

    stronger = PasswordHashingService(time_cost=2, memory_cost=1024, parallelism=1)
    assert stronger.needs_rehash(first)
    stronger.verify_dummy("unknown password")


def test_user_dto_and_model_representation_never_include_password_material(
    database: Session,
) -> None:
    service = IdentityService(database, fast_hasher())
    dto = service.create_local_user("safe@example.com", "valid password")
    row = UserRepository(database).get_by_id(dto.id)

    assert row is not None
    assert row.password_hash is not None
    assert "password_hash" not in dto.model_dump()
    assert "password" not in dto.model_dump()
    assert row.password_hash not in repr(row)
    assert "password_hash" not in repr(row)


@pytest.mark.parametrize(
    ("status", "locked_until", "expected"),
    [
        (UserStatus.PENDING_VERIFICATION, None, "pending_verification"),
        (UserStatus.ACTIVE, None, "valid"),
        (UserStatus.SUSPENDED, None, "suspended"),
        (UserStatus.ANONYMIZED, None, "anonymized"),
        (UserStatus.LOCKED, None, "locked"),
        (UserStatus.LOCKED, datetime.now(UTC) + timedelta(minutes=5), "locked"),
        (UserStatus.LOCKED, datetime.now(UTC) - timedelta(minutes=5), "lock_expired"),
    ],
)
def test_verify_credentials_returns_internal_state_without_persistence(
    database: Session,
    status: UserStatus,
    locked_until: datetime | None,
    expected: str,
) -> None:
    service = IdentityService(database, fast_hasher())
    dto = service.create_local_user("state@example.com", "valid password")
    row = UserRepository(database).get_by_id(dto.id)
    assert row is not None
    row.status = status
    row.locked_until = locked_until
    database.commit()
    before = (
        row.status,
        row.locked_until,
        row.last_login_at,
        row.failed_login_count,
        row.token_version,
    )

    result = service.verify_credentials("STATE@example.com", "valid password")
    database.refresh(row)

    assert result.status == expected
    assert (
        row.status,
        row.locked_until,
        row.last_login_at,
        row.failed_login_count,
        row.token_version,
    ) == before


def test_verify_credentials_handles_missing_user_and_invalid_password(database: Session) -> None:
    service = IdentityService(database, fast_hasher())
    dto = service.create_local_user("known@example.com", "valid password")

    assert service.verify_credentials("missing@example.com", "valid password").status == "invalid_credentials"
    assert service.verify_credentials(dto.email, "incorrect password").status == "invalid_credentials"


def test_missing_user_uses_dummy_verification(database: Session) -> None:
    class DummyTrackingHasher(PasswordHashingService):
        def __init__(self) -> None:
            super().__init__(time_cost=1, memory_cost=1024, parallelism=1)
            self.dummy_calls = 0

        def verify_dummy(self, password: object) -> None:
            self.dummy_calls += 1
            super().verify_dummy(password)

    hasher = DummyTrackingHasher()
    service = IdentityService(database, hasher)

    result = service.verify_credentials("missing@example.com", "valid password")

    assert result.status == "invalid_credentials"
    assert hasher.dummy_calls == 1


def test_active_user_reports_when_password_hash_needs_rehash(database: Session) -> None:
    weak_hasher = fast_hasher()
    user = User(
        email="rehash@example.com",
        email_normalized="rehash@example.com",
        password_hash=weak_hasher.hash("valid password"),
        status=UserStatus.ACTIVE,
    )
    database.add(user)
    database.commit()
    stronger_hasher = PasswordHashingService(
        time_cost=2,
        memory_cost=1024,
        parallelism=1,
    )

    result = IdentityService(database, stronger_hasher).verify_credentials(
        "rehash@example.com",
        "valid password",
    )

    assert result.status == "valid"
    assert result.rehash_required


def test_repository_reuses_session_after_unique_constraint_failure(database: Session) -> None:
    repository = UserRepository(database)
    encoded = fast_hasher().hash("valid password")
    repository.create(
        User(
            email="one@example.com",
            email_normalized="one@example.com",
            password_hash=encoded,
        )
    )

    with pytest.raises(EmailAlreadyExistsError):
        repository.create(
            User(
                email="ONE@example.com",
                email_normalized="one@example.com",
                password_hash=encoded,
            )
        )

    assert repository.exists_by_normalized_email("one@example.com")
    assert database.is_active


def test_repository_does_not_mask_non_unique_integrity_errors(database: Session) -> None:
    repository = UserRepository(database)

    with pytest.raises(IntegrityError):
        repository.create(
            User(
                email="invalid@example.com",
                email_normalized="invalid@example.com",
                password_hash=fast_hasher().hash("valid password"),
                status="not-a-status",
            )
        )

    assert database.is_active

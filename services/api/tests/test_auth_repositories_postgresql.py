"""Real PostgreSQL contracts for P1B repositories and their transaction boundary."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository


PREFIX = "p1b-repository-"


@pytest.fixture(autouse=True)
def cleanup_auth_repository_rows() -> None:
    """Remove only P1B test rows and their dependencies before and after a test."""
    def cleanup() -> None:
        with SessionLocal() as database:
            user_ids = list(database.scalars(text("SELECT id FROM users WHERE email_normalized LIKE :prefix"), {"prefix": f"{PREFIX}%"}))
            if user_ids:
                session_ids = select(AuthSession.id).where(AuthSession.user_id.in_(user_ids))
                database.execute(delete(AuthRefreshToken).where(AuthRefreshToken.session_id.in_(session_ids)))
                database.execute(delete(AuthSession).where(AuthSession.user_id.in_(user_ids)))
                database.execute(delete(User).where(User.id.in_(user_ids)))
            database.commit()
    cleanup()
    try:
        yield
    finally:
        cleanup()


def make_user(suffix: str) -> User:
    email = f"{PREFIX}{suffix}@example.com"
    return User(email=email, email_normalized=email, password_hash="argon2id-test-hash")


def make_session(user_id: UUID, **overrides: object) -> AuthSession:
    values: dict[str, object] = {
        "user_id": user_id,
        "token_version": 1,
        "csrf_secret_hash": "a" * 64,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    values.update(overrides)
    return AuthSession(**values)


def make_refresh(session_id: UUID, **overrides: object) -> AuthRefreshToken:
    values: dict[str, object] = {
        "session_id": session_id,
        "family_id": uuid4(),
        "token_hash": uuid4().hex + uuid4().hex,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    values.update(overrides)
    return AuthRefreshToken(**values)


def persist_graph() -> tuple[UUID, UUID, UUID]:
    with SessionLocal() as database:
        user = make_user(uuid4().hex)
        database.add(user); database.flush()
        session = make_session(user.id)
        database.add(session); database.flush()
        token = make_refresh(session.id)
        database.add(token); database.commit()
        return user.id, session.id, token.id


def test_auth_session_repository_flushes_and_supports_active_lifecycle() -> None:
    with SessionLocal() as database:
        user = make_user("session")
        database.add(user); database.flush()
        repository = AuthSessionRepository(database)
        created = repository.create(make_session(user.id))
        assert created.id is not None and repository.get_by_id(created.id) is created
        assert repository.get_active_by_id(created.id) is created
        repository.update_last_seen(created)
        assert created.last_seen_at is not None
        repository.revoke(created, "logout")
        assert created.revocation_reason == "logout" and repository.get_active_by_id(created.id) is None
        assert repository.revoke_all_by_user(user.id, "security") == 0
        database.rollback()


def test_auth_session_repository_excludes_expired_and_revoked_rows_and_rolls_back() -> None:
    with SessionLocal() as database:
        user = make_user("inactive")
        database.add(user); database.flush()
        repository = AuthSessionRepository(database)
        expired = repository.create(
            make_session(
                user.id,
                created_at=datetime.now(UTC) - timedelta(hours=2),
                updated_at=datetime.now(UTC) - timedelta(hours=2),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        active = repository.create(make_session(user.id))
        assert repository.get_active_by_id(expired.id) is None
        assert [item.id for item in repository.list_active_by_user(user.id)] == [active.id]
        active_id = active.id
        database.rollback()
    with SessionLocal() as verification:
        assert verification.get(AuthSession, active_id) is None


def test_refresh_repository_lifecycle_unique_hash_and_external_rollback() -> None:
    with SessionLocal() as database:
        user = make_user("refresh")
        database.add(user); database.flush()
        session = AuthSessionRepository(database).create(make_session(user.id))
        repository = RefreshTokenRepository(database)
        first = repository.create(make_refresh(session.id))
        successor = repository.create(make_refresh(session.id, parent_token_id=first.id))
        repository.mark_used(first); repository.mark_replaced(first, successor.id)
        assert repository.get_by_hash(first.token_hash) is first
        assert repository.exists_active_successor(first.id)
        assert repository.revoke_family(first.family_id) == 1
        assert repository.revoke_session(session.id) == 1
        duplicate = make_refresh(session.id, token_hash=first.token_hash)
        database.add(duplicate)
        with pytest.raises(IntegrityError):
            database.flush()
        database.rollback()
    with SessionLocal() as verification:
        assert verification.get(AuthSession, session.id) is None


def test_repositories_never_own_commit_or_rollback() -> None:
    class SpySession:
        def __init__(self) -> None:
            self.flush_calls = self.commit_calls = self.rollback_calls = 0
        def add(self, _: object) -> None: pass
        def flush(self) -> None: self.flush_calls += 1
        def execute(self, *_: object) -> object: return type("Result", (), {"rowcount": 0})()

    spy = SpySession()
    user_id = uuid4()
    AuthSessionRepository(spy).create(make_session(user_id))
    RefreshTokenRepository(spy).create(make_refresh(uuid4()))
    assert spy.flush_calls == 2 and spy.commit_calls == 0 and spy.rollback_calls == 0


def test_refresh_get_for_update_locks_row_until_owner_releases_transaction() -> None:
    """PostgreSQL lock timeout proves SELECT FOR UPDATE is not merely compiled SQL."""
    _, _, token_id = persist_graph()
    owner_ready, release_owner = Event(), Event()

    def owner() -> None:
        with SessionLocal() as database:
            token = database.get(AuthRefreshToken, token_id)
            assert token is not None
            assert RefreshTokenRepository(database).get_for_update(token.token_hash) is not None
            owner_ready.set()
            assert release_owner.wait(timeout=5)
            database.rollback()

    def contender() -> str:
        assert owner_ready.wait(timeout=5)
        with SessionLocal() as database:
            database.execute(text("SET LOCAL lock_timeout = '250ms'"))
            token_hash = database.scalar(text("SELECT token_hash FROM auth_refresh_tokens WHERE id = :id"), {"id": token_id})
            try:
                RefreshTokenRepository(database).get_for_update(token_hash)
            except DBAPIError:
                database.rollback()
                return "blocked"
            return "unexpected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner_future = executor.submit(owner)
        contender_future = executor.submit(contender)
        assert contender_future.result(timeout=5) == "blocked"
        release_owner.set()
        owner_future.result(timeout=5)

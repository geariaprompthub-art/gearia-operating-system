"""Real PostgreSQL locking contracts for P2B lifecycle challenges."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.lifecycle_tokens import EmailVerificationToken
from app.models.user import User
from app.services.lifecycle_token_service import InvalidLifecycleTokenError, LifecycleTokenService


PREFIX = "p2b-token-"


def _user() -> User:
    with SessionLocal() as database:
        row = User(email=f"{PREFIX}{uuid4().hex}@example.com", email_normalized=f"{PREFIX}{uuid4().hex}@invalid.example", password_hash="hash")
        database.add(row); database.commit(); database.refresh(row)
        return row


def test_postgresql_concurrent_consumption_allows_exactly_one_success() -> None:
    user = _user()
    try:
        with SessionLocal() as database:
            issued = LifecycleTokenService(database, "test-pepper").issue_email_verification(user.id, timedelta(minutes=5)); database.commit()
        barrier = Barrier(2)
        def consume() -> str:
            with SessionLocal() as database:
                barrier.wait(timeout=10)
                try:
                    LifecycleTokenService(database, "test-pepper").consume_email_verification(issued.raw_token)
                    database.commit(); return "consumed"
                except InvalidLifecycleTokenError:
                    database.rollback(); return "rejected"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: consume(), range(2)))
        assert sorted(outcomes) == ["consumed", "rejected"]
    finally:
        with SessionLocal() as database:
            database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)); database.execute(delete(User).where(User.id == user.id)); database.commit()


def test_postgresql_concurrent_issuance_leaves_exactly_one_active_token() -> None:
    user = _user()
    barrier = Barrier(2)
    try:
        def issue() -> str:
            with SessionLocal() as database:
                barrier.wait(timeout=10)
                LifecycleTokenService(database, "test-pepper").issue_email_verification(user.id, timedelta(minutes=5))
                database.commit()
                return "issued"
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(lambda _: issue(), range(2))) == ["issued", "issued"]
        with SessionLocal() as database:
            rows = list(database.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)))
            assert len(rows) == 2
            assert sum(row.invalidated_at is None and row.used_at is None for row in rows) == 1
            assert len({row.token_hash for row in rows}) == 2
    finally:
        with SessionLocal() as database:
            database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)); database.execute(delete(User).where(User.id == user.id)); database.commit()

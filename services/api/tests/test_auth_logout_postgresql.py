"""PostgreSQL contracts for the P1B logout transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.user import User, UserStatus
from app.repositories.auth_session_repository import AuthSessionRepository
from app.services.access_token_authenticator import AccessTokenAuthenticator
from app.services.auth_service import InvalidRefreshError

from tests.test_auth_refresh_postgresql import PREFIX, fast_hasher, make_service, postgres_jwt


@pytest.fixture(autouse=True)
def cleanup_logout_rows() -> None:
    def cleanup() -> None:
        with SessionLocal() as database:
            user_ids = list(database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))))
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


def test_postgresql_logout_revokes_only_selected_session() -> None:
    database = SessionLocal()
    try:
        email = f"{PREFIX}{uuid4().hex}@example.com"
        user = User(email=email, email_normalized=email, password_hash=fast_hasher().hash("valid password"), status=UserStatus.ACTIVE)
        database.add(user); database.commit()
        jwt = postgres_jwt(); service = make_service(database, jwt)
        first, second = service.login(email, "valid password"), service.login(email, "valid password")
        principal = AccessTokenAuthenticator(database, jwt).authenticate(first.access_token)
        service.logout(principal, first.csrf_token, first.csrf_token)
        database.expire_all()
        first_session, second_session = database.get(AuthSession, first.session_id), database.get(AuthSession, second.session_id)
        first_tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == first.session_id)))
        second_tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == second.session_id)))
        assert first_session is not None and first_session.revocation_reason == "user_logout"
        assert second_session is not None and second_session.revoked_at is None
        assert first_tokens and all(item.revoked_at is not None for item in first_tokens)
        assert second_tokens and all(item.revoked_at is None for item in second_tokens)
    finally:
        database.close()


def test_postgresql_concurrent_logout_is_idempotent_without_partial_state() -> None:
    database = SessionLocal()
    try:
        email = f"{PREFIX}{uuid4().hex}@example.com"
        user = User(email=email, email_normalized=email, password_hash=fast_hasher().hash("valid password"), status=UserStatus.ACTIVE)
        database.add(user); database.commit()
        jwt = postgres_jwt(); setup = make_service(database, jwt)
        login = setup.login(email, "valid password")
    finally:
        database.close()

    barrier = Barrier(2)
    def logout() -> bool:
        local = SessionLocal()
        try:
            service = make_service(local, jwt)
            principal = AccessTokenAuthenticator(local, jwt).authenticate(login.access_token)
            barrier.wait(timeout=5)
            return service.logout(principal, login.csrf_token, login.csrf_token).already_revoked
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: logout(), range(2)))
    with SessionLocal() as verification:
        session = verification.get(AuthSession, login.session_id)
        tokens = list(verification.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id)))
        assert sorted(outcomes) == [False, True]
        assert session is not None and session.revocation_reason == "user_logout"
        assert tokens and all(token.revoked_at is not None for token in tokens)


def test_postgresql_logout_then_refresh_never_leaves_an_active_token() -> None:
    database = SessionLocal()
    try:
        email = f"{PREFIX}{uuid4().hex}@example.com"
        user = User(email=email, email_normalized=email, password_hash=fast_hasher().hash("valid password"), status=UserStatus.ACTIVE)
        database.add(user); database.commit()
        jwt = postgres_jwt(); setup = make_service(database, jwt)
        login = setup.login(email, "valid password")
        principal = AccessTokenAuthenticator(database, jwt).authenticate(login.access_token)
    finally:
        database.close()

    locked, refresh_started = Event(), Event()
    class CoordinatedRepository(AuthSessionRepository):
        def get_by_id_for_update(self, session_id):  # type: ignore[no-untyped-def]
            session = super().get_by_id_for_update(session_id)
            locked.set()
            assert refresh_started.wait(timeout=5)
            return session
    def logout() -> str:
        local = SessionLocal()
        try:
            service = make_service(local, jwt)
            service._session_repository = CoordinatedRepository(local)
            service.logout(principal, login.csrf_token, login.csrf_token)
            return "logout"
        finally:
            local.close()
    def refresh() -> str:
        local = SessionLocal()
        try:
            assert locked.wait(timeout=5)
            refresh_started.set()
            try:
                make_service(local, jwt).refresh(login.refresh_token, login.csrf_token, login.csrf_token, "127.0.0.1")
                return "refreshed"
            except InvalidRefreshError:
                return "rejected"
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda operation: operation(), (logout, refresh)))
    with SessionLocal() as verification:
        session = verification.get(AuthSession, login.session_id)
        active = list(verification.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id, AuthRefreshToken.revoked_at.is_(None))))
        assert "logout" in outcomes
        assert session is not None and session.revoked_at is not None
        assert active == []

"""P1B phase-six logout service and HTTP contracts using an isolated database."""

import logging
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base
from app.main import app
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.access_token_authenticator import AccessTokenAuthenticator
from app.services.auth_dependencies import get_auth_service
from app.services.auth_service import (
    AuthService,
    InvalidLogoutCsrfError,
    LogoutResult,
)
from app.services.principal_dependencies import get_access_token_authenticator

from tests.test_auth_login import SessionLocal, create_user, engine, make_service


@pytest.fixture(autouse=True)
def isolated_logout_database() -> None:
    """Keep logout tests independent of the phase-three SQLite fixture."""

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        Base.metadata.drop_all(bind=engine)


def _principal(database: object, service: AuthService, access_token: str) -> object:
    return AccessTokenAuthenticator(database, service._jwt_service).authenticate(access_token)  # type: ignore[arg-type]


def test_logout_service_revokes_only_current_session_refresh_tokens_and_is_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.auth_service")
    with SessionLocal() as database:
        user = create_user(database)
        service = make_service(database)
        first = service.login(user.email, "valid password")
        second = service.login(user.email, "valid password")
        principal = _principal(database, service, first.access_token)

        result = service.logout(principal, first.csrf_token, first.csrf_token)
        repeated = service.logout(principal, first.csrf_token, first.csrf_token)
        first_session = database.get(AuthSession, first.session_id)
        second_session = database.get(AuthSession, second.session_id)
        first_tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == first.session_id)))
        second_tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == second.session_id)))

        assert isinstance(result, LogoutResult) and not result.already_revoked
        assert repeated.already_revoked and repeated.revoked_at == result.revoked_at
        assert first_session is not None and first_session.revoked_at is not None
        assert first_session.revocation_reason == "user_logout"
        assert second_session is not None and second_session.revoked_at is None
        assert first_tokens and all(token.revoked_at is not None for token in first_tokens)
        assert second_tokens and all(token.revoked_at is None for token in second_tokens)
        assert "logout_success" in caplog.text and "logout_already_revoked" in caplog.text
        for secret in (first.access_token, first.refresh_token, first.csrf_token):
            assert secret not in caplog.text


def test_logout_rejects_csrf_without_partial_revocation() -> None:
    with SessionLocal() as database:
        user = create_user(database)
        service = make_service(database)
        login = service.login(user.email, "valid password")
        principal = _principal(database, service, login.access_token)

        with pytest.raises(InvalidLogoutCsrfError):
            service.logout(principal, login.csrf_token, "mismatched")

        session = database.get(AuthSession, login.session_id)
        tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id)))
        assert session is not None and session.revoked_at is None
        assert tokens and all(token.revoked_at is None for token in tokens)


def test_logout_rolls_back_when_refresh_token_revocation_fails() -> None:
    class FailingRefreshTokenRepository(RefreshTokenRepository):
        def revoke_session(self, session_id: object, reason: str = "session_revoked") -> int:
            raise RuntimeError("test refresh revocation failure")

    with SessionLocal() as database:
        user = create_user(database)
        service = make_service(database)
        login = service.login(user.email, "valid password")
        principal = _principal(database, service, login.access_token)
        service._refresh_token_repository = FailingRefreshTokenRepository(database)

        with pytest.raises(RuntimeError, match="test refresh revocation failure"):
            service.logout(principal, login.csrf_token, login.csrf_token)

        session = database.get(AuthSession, login.session_id)
        tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id)))
        assert session is not None and session.revoked_at is None
        assert tokens and all(token.revoked_at is None for token in tokens)


def test_logout_http_clears_cookies_and_invalidates_only_the_current_session() -> None:
    database = SessionLocal()
    user = create_user(database)
    service = make_service(database)
    first = service.login(user.email, "valid password")
    second = service.login(user.email, "valid password")
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_access_token_authenticator] = lambda: AccessTokenAuthenticator(database, service._jwt_service)
    try:
        client = TestClient(app)
        client.cookies.set("gearia_access", first.access_token)
        client.cookies.set("gearia_refresh", first.refresh_token)
        client.cookies.set("gearia_csrf", first.csrf_token)
        response = client.post("/auth/logout", headers={"X-CSRF-Token": first.csrf_token})
        rendered = "\n".join(response.headers.get_list("set-cookie"))
        assert response.status_code == 204 and response.content == b""
        assert all(name in rendered for name in ("gearia_access=", "gearia_refresh=", "gearia_csrf="))
        assert all("Max-Age=0" in value for value in response.headers.get_list("set-cookie"))
        assert "Cache-Control" in response.headers and "token" not in response.text.lower()

        old_access = first.access_token
        client.cookies.set("gearia_access", old_access)
        assert client.get("/auth/me").status_code == 401
        client.cookies.set("gearia_refresh", first.refresh_token)
        client.cookies.set("gearia_csrf", first.csrf_token)
        assert client.post("/auth/refresh", headers={"X-CSRF-Token": first.csrf_token}).status_code == 401

        second_principal = _principal(database, service, second.access_token)
        assert second_principal.session_id == second.session_id
        assert database.get(AuthSession, second.session_id).revoked_at is None  # type: ignore[union-attr]
    finally:
        app.dependency_overrides.clear()
        database.close()


def test_logout_http_rejects_missing_or_invalid_csrf_without_clearing_cookies() -> None:
    database = SessionLocal()
    user = create_user(database)
    service = make_service(database)
    login = service.login(user.email, "valid password")
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_access_token_authenticator] = lambda: AccessTokenAuthenticator(database, service._jwt_service)
    try:
        client = TestClient(app)
        client.cookies.set("gearia_access", login.access_token)
        client.cookies.set("gearia_csrf", login.csrf_token)
        response = client.post("/auth/logout")
        assert response.status_code == 403 and response.json() == {"detail": "Logout failed"}
        assert "set-cookie" not in response.headers
        assert database.get(AuthSession, login.session_id).revoked_at is None  # type: ignore[union-attr]
    finally:
        app.dependency_overrides.clear()
        database.close()

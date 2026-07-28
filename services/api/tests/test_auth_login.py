"""P1B phase-three login service and HTTP contracts; no refresh/logout flow."""

from datetime import UTC, datetime, timedelta
import logging
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.main import app
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.user import User, UserStatus
from app.routers.auth import router
from app.services.auth_dependencies import get_auth_service
from app.services.access_token_authenticator import AccessTokenAuthenticator
from app.services.principal_dependencies import get_access_token_authenticator
from app.services.auth_service import AuthService
from app.services.auth_service import (
    InvalidCsrfError,
    InvalidRefreshError,
    RefreshReuseDetectedError,
)
from app.services.cookie_policy import CookiePolicy
from app.services.csrf_service import CsrfService
from app.services.identity_service import IdentityService
from app.services.jwt_service import JWTService
from app.services.password_hasher import PasswordHashingService
from app.services.rate_limiter import RateLimitDecision, RateLimitPolicy
from app.services.refresh_token_service import RefreshTokenService
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class ControlledRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[RateLimitPolicy, str]] = []

    def consume(self, policy: RateLimitPolicy, identifier: str) -> RateLimitDecision:
        self.calls.append((policy, identifier))
        return RateLimitDecision(self.allowed, 4 if self.allowed else 0, 100, 60 if not self.allowed else 0)


class FailingJWT:
    def issue(self, *_: object) -> str:
        raise RuntimeError("test signing failure")


def fast_hasher() -> PasswordHashingService:
    return PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)


def jwt_service() -> JWTService:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return JWTService(private, public, "login-test-kid", "login-issuer", "login-audience", 300)


def make_service(database: Session, *, rate_limiter: ControlledRateLimiter | None = None, signer: object | None = None) -> AuthService:
    return AuthService(
        database,
        IdentityService(database, fast_hasher()),
        signer or jwt_service(),
        RefreshTokenService(),
        CookiePolicy(False, "lax", None, 300, 3600),
        CsrfService(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        rate_limiter or ControlledRateLimiter(),
        RateLimitPolicy("auth:login", 5, 60),
        3600,
    )


def create_user(database: Session, *, email: str = "login@example.com", status: str = UserStatus.ACTIVE, password: str = "valid password") -> User:
    user = User(email=email, email_normalized=email.casefold(), password_hash=fast_hasher().hash(password), status=status)
    if status == UserStatus.LOCKED:
        user.locked_until = datetime.now(UTC) + timedelta(minutes=10)
    database.add(user); database.commit(); database.refresh(user)
    return user


@pytest.fixture(autouse=True)
def isolated_auth_database() -> None:
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


def test_login_service_commits_one_session_refresh_jwt_and_csrf() -> None:
    with SessionLocal() as database:
        user = create_user(database)
        service = make_service(database)
        result = service.login(" LOGIN@EXAMPLE.COM ", "valid password")
        assert result.user_id == user.id and result.email == user.email
        assert result.access_token not in repr(result) and result.refresh_token not in repr(result)
        claims = service._jwt_service.validate(result.access_token)
        assert claims.user_id == user.id and claims.session_id == result.session_id
        session = database.get(AuthSession, result.session_id)
        refresh = database.scalar(select(AuthRefreshToken).where(AuthRefreshToken.session_id == result.session_id))
        assert session is not None and refresh is not None
        assert refresh.token_hash == RefreshTokenService().hash(result.refresh_token)
        assert result.refresh_token not in refresh.token_hash and CsrfService().valid(result.csrf_token, session.csrf_secret_hash)


def test_login_http_emits_only_cookies_and_sanitized_public_payload() -> None:
    database = SessionLocal(); user = create_user(database)
    expected_id, expected_email = user.id, user.email
    service = make_service(database)
    app.dependency_overrides[get_auth_service] = lambda: service
    try:
        response = TestClient(app).post("/auth/login", json={"email": user.email, "password": "valid password"})
    finally:
        app.dependency_overrides.clear(); database.close()
    rendered = "\n".join(response.headers.get_list("set-cookie"))
    assert response.status_code == 200
    assert response.json() == {"user": {"id": str(expected_id), "email": expected_email}}
    assert all(name in rendered for name in ("gearia_access=", "gearia_refresh=", "gearia_csrf="))
    assert "HttpOnly" in rendered and "Cache-Control" in response.headers
    for forbidden in ("access_token", "refresh_token", "csrf_token", "password", "token_hash"):
        assert forbidden not in response.text


@pytest.mark.parametrize("status", [UserStatus.PENDING_VERIFICATION, UserStatus.SUSPENDED, UserStatus.LOCKED, UserStatus.ANONYMIZED])
def test_login_rejects_non_active_accounts_without_creating_session(status: str) -> None:
    database = SessionLocal(); user = create_user(database, status=status)
    app.dependency_overrides[get_auth_service] = lambda: make_service(database)
    try:
        response = TestClient(app).post("/auth/login", json={"email": user.email, "password": "valid password"})
        assert response.status_code == 403 and response.json() == {"detail": "Account cannot sign in"}
        assert database.scalar(select(AuthSession.id)) is None
    finally:
        app.dependency_overrides.clear(); database.close()


def test_login_invalid_password_and_unknown_user_are_indistinguishable() -> None:
    database = SessionLocal(); user = create_user(database)
    app.dependency_overrides[get_auth_service] = lambda: make_service(database)
    try:
        client = TestClient(app)
        wrong = client.post("/auth/login", json={"email": user.email, "password": "wrong password"})
        unknown = client.post("/auth/login", json={"email": "unknown@example.com", "password": "wrong password"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json() == {"detail": "Invalid credentials"}
        assert database.scalar(select(AuthSession.id)) is None
    finally:
        app.dependency_overrides.clear(); database.close()


def test_login_rate_limit_and_rollback_never_leave_partial_security_records() -> None:
    database = SessionLocal(); user = create_user(database)
    denied = ControlledRateLimiter(False)
    app.dependency_overrides[get_auth_service] = lambda: make_service(database, rate_limiter=denied)
    try:
        response = TestClient(app).post("/auth/login", json={"email": user.email, "password": "valid password"})
        assert response.status_code == 429 and len(denied.calls) == 1
        assert database.scalar(select(AuthSession.id)) is None
    finally:
        app.dependency_overrides.clear()
    app.dependency_overrides[get_auth_service] = lambda: make_service(database, signer=FailingJWT())
    try:
        response = TestClient(app, raise_server_exceptions=False).post("/auth/login", json={"email": user.email, "password": "valid password"})
        assert response.status_code == 500 and response.json() == {"detail": "Authentication service unavailable"}
        assert database.scalar(select(AuthSession.id)) is None
        assert database.scalar(select(AuthRefreshToken.id)) is None
    finally:
        app.dependency_overrides.clear(); database.close()


@pytest.mark.parametrize("payload", [
    {}, {"email": 1, "password": "valid password"}, {"email": "a@b.com", "password": True},
    {"email": "a@b.com", "password": "valid password", "extra": "no"},
])
def test_login_http_contract_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    app.dependency_overrides[get_auth_service] = lambda: object()
    try:
        response = TestClient(app).post("/auth/login", json=payload)
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_refresh_service_rotates_lineage_jwt_and_csrf_in_one_transaction(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services.auth_service")
    with SessionLocal() as database:
        create_user(database)
        service = make_service(database)
        login = service.login("login@example.com", "valid password")
        before = service._jwt_service.validate(login.access_token)
        result = service.refresh(login.refresh_token, login.csrf_token, login.csrf_token, "127.0.0.1")
        after = service._jwt_service.validate(result.access_token)
        original = database.scalar(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id, AuthRefreshToken.parent_token_id.is_(None)))
        successor = database.scalar(select(AuthRefreshToken).where(AuthRefreshToken.id != original.id))
        session = database.get(AuthSession, login.session_id)
        assert original is not None and successor is not None and session is not None
        assert original.used_at is not None and original.replaced_by_token_id == successor.id
        assert successor.parent_token_id == original.id and successor.family_id == original.family_id
        assert successor.revoked_at is None and session.last_seen_at is not None
        assert before.jti != after.jti and result.refresh_token != login.refresh_token
        assert CsrfService().valid(result.csrf_token, session.csrf_secret_hash)
        rendered_logs = caplog.text
        assert "refresh_success" in rendered_logs
        assert login.refresh_token not in rendered_logs and result.refresh_token not in rendered_logs
        assert login.csrf_token not in rendered_logs and result.csrf_token not in rendered_logs


def test_refresh_rejects_invalid_csrf_or_token_without_rotation_and_revokes_on_reuse() -> None:
    with SessionLocal() as database:
        create_user(database)
        service = make_service(database)
        login = service.login("login@example.com", "valid password")
        with pytest.raises(InvalidCsrfError):
            service.refresh(login.refresh_token, login.csrf_token, "other", "127.0.0.1")
        with pytest.raises(InvalidRefreshError):
            service.refresh("malformed", login.csrf_token, login.csrf_token, "127.0.0.1")
        rotated = service.refresh(login.refresh_token, login.csrf_token, login.csrf_token, "127.0.0.1")
        with pytest.raises(RefreshReuseDetectedError):
            service.refresh(login.refresh_token, rotated.csrf_token, rotated.csrf_token, "127.0.0.1")
        session = database.get(AuthSession, login.session_id)
        tokens = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id)))
        assert session is not None and session.revoked_at is not None
        assert all(token.revoked_at is not None for token in tokens)
        assert any(token.reuse_detected_at is not None for token in tokens)


@pytest.mark.parametrize("state", ["expired_token", "revoked_token", "expired_session", "revoked_session", "suspended_user"])
def test_refresh_rejects_terminal_token_session_and_user_states(state: str) -> None:
    with SessionLocal() as database:
        user = create_user(database)
        service = make_service(database)
        login = service.login(user.email, "valid password")
        token = database.scalar(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id))
        session = database.get(AuthSession, login.session_id)
        assert token is not None and session is not None
        if state == "expired_token":
            token.created_at = datetime.now(UTC) - timedelta(hours=2)
            token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif state == "revoked_token":
            token.revoked_at = datetime.now(UTC)
        elif state == "expired_session":
            session.created_at = datetime.now(UTC) - timedelta(hours=2)
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif state == "revoked_session":
            session.revoked_at = datetime.now(UTC)
        else:
            user.status = UserStatus.SUSPENDED
        database.commit()
        with pytest.raises(InvalidRefreshError):
            service.refresh(login.refresh_token, login.csrf_token, login.csrf_token, "127.0.0.1")


def test_refresh_rejects_validly_shaped_token_with_incorrect_hash() -> None:
    with SessionLocal() as database:
        create_user(database)
        service = make_service(database)
        login = service.login("login@example.com", "valid password")
        token_id, secret = login.refresh_token.split(".")
        tampered = f"{token_id}.{'A' if secret[0] != 'A' else 'B'}{secret[1:]}"
        with pytest.raises(InvalidRefreshError):
            service.refresh(tampered, login.csrf_token, login.csrf_token, "127.0.0.1")


def test_refresh_rollback_emits_no_successor_when_jwt_fails() -> None:
    with SessionLocal() as database:
        create_user(database)
        service = make_service(database)
        login = service.login("login@example.com", "valid password")
        service._jwt_service = FailingJWT()
        with pytest.raises(RuntimeError):
            service.refresh(login.refresh_token, login.csrf_token, login.csrf_token, "127.0.0.1")
        token = database.scalar(select(AuthRefreshToken).where(AuthRefreshToken.session_id == login.session_id))
        assert token is not None and token.used_at is None and token.replaced_by_token_id is None
        assert database.scalar(select(AuthRefreshToken).where(AuthRefreshToken.parent_token_id == token.id)) is None


def test_refresh_http_renews_cookies_and_hides_tokens() -> None:
    database = SessionLocal(); create_user(database)
    service = make_service(database); login = service.login("login@example.com", "valid password")
    app.dependency_overrides[get_auth_service] = lambda: service
    try:
        client = TestClient(app)
        client.cookies.set("gearia_refresh", login.refresh_token)
        client.cookies.set("gearia_csrf", login.csrf_token)
        response = client.post("/auth/refresh", headers={"X-CSRF-Token": login.csrf_token})
        rendered = "\n".join(response.headers.get_list("set-cookie"))
        assert response.status_code == 200 and response.json() == {"status": "authenticated"}
        assert all(name in rendered for name in ("gearia_access=", "gearia_refresh=", "gearia_csrf="))
        for forbidden in ("access_token", "refresh_token", "csrf_token", "token_hash", "family_id"):
            assert forbidden not in response.text
    finally:
        app.dependency_overrides.clear(); database.close()


def test_refresh_http_errors_are_uniform_clear_terminal_cookies_and_rate_limit() -> None:
    database = SessionLocal(); create_user(database)
    service = make_service(database)
    app.dependency_overrides[get_auth_service] = lambda: service
    try:
        client = TestClient(app)
        missing = client.post("/auth/refresh")
        assert missing.status_code == 401 and missing.json() == {"detail": "Refresh failed"}
        assert "Max-Age=0" in "\n".join(missing.headers.get_list("set-cookie"))
    finally:
        app.dependency_overrides.clear()
    denied = make_service(database, rate_limiter=ControlledRateLimiter(False))
    app.dependency_overrides[get_auth_service] = lambda: denied
    try:
        client = TestClient(app)
        client.cookies.set("gearia_refresh", "malformed")
        client.cookies.set("gearia_csrf", "csrf")
        limited = client.post("/auth/refresh", headers={"X-CSRF-Token": "csrf"})
        assert limited.status_code == 429 and "Retry-After" in limited.headers
    finally:
        app.dependency_overrides.clear(); database.close()


def test_current_user_uses_only_access_cookie_and_performs_no_writes() -> None:
    database = SessionLocal(); user = create_user(database)
    service = make_service(database); login = service.login(user.email, "valid password")
    session = database.get(AuthSession, login.session_id)
    assert session is not None
    before = (session.last_seen_at, session.updated_at, session.csrf_secret_hash)
    app.dependency_overrides[get_access_token_authenticator] = lambda: AccessTokenAuthenticator(database, service._jwt_service)
    try:
        client = TestClient(app)
        client.cookies.set("gearia_access", login.access_token)
        response = client.get("/auth/me")
        assert response.status_code == 200
        assert response.json() == {
            "id": str(user.id), "email": user.email, "status": "active",
            "email_verified_at": None, "created_at": user.created_at.isoformat().replace("+00:00", "Z"),
        }
        assert "set-cookie" not in response.headers
        for forbidden in ("session_id", "token_version", "token_jti", "password_hash", "refresh", "csrf"):
            assert forbidden not in response.text
        database.refresh(session)
        assert (session.last_seen_at, session.updated_at, session.csrf_secret_hash) == before
    finally:
        app.dependency_overrides.clear(); database.close()


@pytest.mark.parametrize("token", [None, "", "malformed.token.value"])
def test_current_user_rejects_missing_or_invalid_access_cookie_uniformly(token: str | None) -> None:
    database = SessionLocal(); service = make_service(database)
    app.dependency_overrides[get_access_token_authenticator] = lambda: AccessTokenAuthenticator(database, service._jwt_service)
    try:
        client = TestClient(app)
        if token is not None:
            client.cookies.set("gearia_access", token)
        response = client.get("/auth/me")
        assert response.status_code == 401 and response.json() == {"detail": "Authentication required"}
        assert "set-cookie" not in response.headers
    finally:
        app.dependency_overrides.clear(); database.close()


@pytest.mark.parametrize("state", ["revoked_session", "expired_session", "suspended_user", "token_version"])
def test_current_user_rejects_invalid_persisted_authentication_state(state: str) -> None:
    database = SessionLocal(); user = create_user(database)
    service = make_service(database); login = service.login(user.email, "valid password")
    session = database.get(AuthSession, login.session_id)
    assert session is not None
    if state == "revoked_session":
        session.revoked_at = datetime.now(UTC)
    elif state == "expired_session":
        session.created_at = datetime.now(UTC) - timedelta(hours=2)
        session.expires_at = datetime.now(UTC) - timedelta(hours=1)
    elif state == "suspended_user":
        user.status = UserStatus.SUSPENDED
    else:
        user.token_version += 1
    database.commit()
    app.dependency_overrides[get_access_token_authenticator] = lambda: AccessTokenAuthenticator(database, service._jwt_service)
    try:
        client = TestClient(app); client.cookies.set("gearia_access", login.access_token)
        response = client.get("/auth/me")
        assert response.status_code == 401 and response.json() == {"detail": "Authentication required"}
        assert "set-cookie" not in response.headers
    finally:
        app.dependency_overrides.clear(); database.close()

"""PostgreSQL-only refresh rotation and replay concurrency contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.auth_refresh_token import AuthRefreshToken
from app.models.auth_session import AuthSession
from app.models.user import User, UserStatus
from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services.auth_service import AuthService, RefreshReuseDetectedError
from app.services.access_token_authenticator import AccessAuthenticationError, AccessTokenAuthenticator
from app.services.cookie_policy import CookiePolicy
from app.services.csrf_service import CsrfService
from app.services.identity_service import IdentityService
from app.services.jwt_service import JWTService
from app.services.password_hasher import PasswordHashingService
from app.services.rate_limiter import RateLimitDecision, RateLimitPolicy
from app.services.refresh_token_service import RefreshTokenService


PREFIX = "p1b-refresh-postgres-"


class PermissiveRateLimiter:
    def consume(self, _: RateLimitPolicy, __: object) -> RateLimitDecision:
        return RateLimitDecision(True, 99, 0, 0)


class JwtStub:
    def __init__(self) -> None:
        self.calls = 0

    def issue(self, *_: object) -> str:
        self.calls += 1
        return f"test-access-{self.calls}"


def fast_hasher() -> PasswordHashingService:
    return PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)


@pytest.fixture(autouse=True)
def cleanup_refresh_rows() -> None:
    """Remove only clearly named integration fixtures, preserving baseline data."""
    def cleanup() -> None:
        with SessionLocal() as database:
            users = list(database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))))
            if users:
                session_ids = select(AuthSession.id).where(AuthSession.user_id.in_(users))
                database.execute(delete(AuthRefreshToken).where(AuthRefreshToken.session_id.in_(session_ids)))
                database.execute(delete(AuthSession).where(AuthSession.user_id.in_(users)))
                database.execute(delete(User).where(User.id.in_(users)))
            database.commit()
    cleanup()
    try:
        yield
    finally:
        cleanup()


def make_service(database: object, jwt_service: object | None = None) -> AuthService:
    return AuthService(
        database,
        IdentityService(database, fast_hasher()),
        jwt_service or JwtStub(),
        RefreshTokenService(),
        CookiePolicy(False, "lax", None, 300, 3600),
        CsrfService(),
        AuthSessionRepository(database),
        RefreshTokenRepository(database),
        PermissiveRateLimiter(),
        RateLimitPolicy("auth:login", 5, 60),
        3600,
        RateLimitPolicy("auth:refresh", 20, 60),
        300,
    )


def create_login() -> tuple[str, str, object]:
    database = SessionLocal()
    email = f"{PREFIX}{uuid4().hex}@example.com"
    user = User(email=email, email_normalized=email, password_hash=fast_hasher().hash("valid password"), status=UserStatus.ACTIVE)
    database.add(user); database.commit()
    result = make_service(database).login(email, "valid password")
    database.close()
    return result.refresh_token, result.csrf_token, result.session_id


def test_postgresql_refresh_rotation_persists_one_active_successor_and_rolls_back_failures() -> None:
    raw_token, csrf, session_id = create_login()
    with SessionLocal() as database:
        service = make_service(database)
        rotated = service.refresh(raw_token, csrf, csrf, "127.0.0.1")
        records = list(database.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == session_id)))
        original = next(item for item in records if item.parent_token_id is None)
        successor = next(item for item in records if item.parent_token_id == original.id)
        assert len(records) == 2 and original.used_at is not None
        assert original.replaced_by_token_id == successor.id
        assert successor.family_id == original.family_id and successor.revoked_at is None
        assert RefreshTokenService().hash(rotated.refresh_token) == successor.token_hash

    with SessionLocal() as verification:
        assert verification.scalar(select(AuthRefreshToken).where(AuthRefreshToken.session_id == session_id, AuthRefreshToken.revoked_at.is_(None)).limit(2)) is not None


def test_postgresql_concurrent_replay_creates_at_most_one_successor_then_revokes_session() -> None:
    raw_token, csrf, session_id = create_login()
    barrier = Barrier(2)

    def rotate() -> str:
        database = SessionLocal()
        try:
            service = make_service(database)
            barrier.wait(timeout=5)
            try:
                service.refresh(raw_token, csrf, csrf, "127.0.0.1")
                return "success"
            except RefreshReuseDetectedError:
                return "reuse"
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: rotate(), range(2)))

    with SessionLocal() as verification:
        session = verification.get(AuthSession, session_id)
        tokens = list(verification.scalars(select(AuthRefreshToken).where(AuthRefreshToken.session_id == session_id)))
        active = [token for token in tokens if token.revoked_at is None and token.used_at is None]
        assert sorted(outcomes) == ["reuse", "success"]
        assert session is not None and session.revoked_at is not None
        assert len(tokens) == 2 and not active
        assert sum(token.reuse_detected_at is not None for token in tokens) == 1


def postgres_jwt() -> JWTService:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return JWTService(private, public, "principal-test", "principal-issuer", "principal-audience", 300)


def test_postgresql_authenticated_principal_validates_session_user_and_token_version_without_writes() -> None:
    with SessionLocal() as database:
        email = f"{PREFIX}{uuid4().hex}@example.com"
        user = User(email=email, email_normalized=email, password_hash="argon2id-test", status=UserStatus.ACTIVE)
        database.add(user); database.flush()
        session = AuthSession(user_id=user.id, token_version=1, csrf_secret_hash="a" * 64, expires_at=datetime.now(UTC) + timedelta(hours=1))
        database.add(session); database.commit(); database.refresh(session)
        before = (session.last_seen_at, session.updated_at, session.csrf_secret_hash)
        jwt_service = postgres_jwt()
        token = jwt_service.issue(user.id, session.id, 1)
        principal = AccessTokenAuthenticator(database, jwt_service).authenticate(token)
        database.refresh(session)
        assert principal.user_id == user.id and principal.session_id == session.id
        assert (session.last_seen_at, session.updated_at, session.csrf_secret_hash) == before
        user.token_version = 2; database.commit()
        with pytest.raises(AccessAuthenticationError):
            AccessTokenAuthenticator(database, jwt_service).authenticate(token)

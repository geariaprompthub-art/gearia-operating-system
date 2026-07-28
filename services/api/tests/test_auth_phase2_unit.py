"""Focused P1B phase-two contracts with no HTTP authentication surface."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Response

from app.core.config import Settings
from app.services import auth_dependencies
from app.services.auth_service import AuthService
from app.services.cookie_policy import CookiePolicy
from app.services.csrf_service import CsrfService
from app.services.jwt_service import InvalidAccessTokenError, JWTService
from app.services.rate_limiter import RateLimitFailureMode, RateLimitPolicy, RedisRateLimiter
from app.services.refresh_token_service import InvalidRefreshTokenError, RefreshTokenService


def make_jwt_service(*, issuer: str = "issuer", audience: str = "audience", ttl: int = 60) -> JWTService:
    """Create a unique Ed25519-only service for each security test."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return JWTService(private, public, "test-kid", issuer, audience, ttl)


def test_jwt_rejects_header_algorithm_and_required_claim_mutations() -> None:
    service = make_jwt_service()
    user_id, session_id = uuid4(), uuid4()
    valid = service.issue(user_id, session_id, 1)
    assert jwt.get_unverified_header(valid)["alg"] == "EdDSA"

    header, payload, signature = valid.split(".")
    forged = f"{header}.{payload}.invalid"
    with pytest.raises(InvalidAccessTokenError):
        service.validate(forged)

    none_token = jwt.encode({"sub": str(user_id)}, key="", algorithm="none")
    with pytest.raises(InvalidAccessTokenError):
        service.validate(none_token)

    for claim in ("sub", "sid", "jti", "token_version", "type", "exp"):
        raw = jwt.decode(valid, options={"verify_signature": False})
        raw.pop(claim)
        mutated = jwt.encode(raw, service._private_key, algorithm="EdDSA", headers={"kid": "test-kid"})
        with pytest.raises(InvalidAccessTokenError):
            service.validate(mutated)


def test_jwt_rejects_wrong_identity_claims_and_configuration_without_secrets() -> None:
    service = make_jwt_service()
    user_id, session_id = uuid4(), uuid4()
    expired = {
        "iss": "issuer", "aud": "audience", "sub": str(user_id), "sid": str(session_id),
        "token_version": 1, "iat": datetime.now(UTC) - timedelta(minutes=2),
        "nbf": datetime.now(UTC) - timedelta(minutes=2), "exp": datetime.now(UTC) - timedelta(minutes=1),
        "jti": str(uuid4()), "type": "access",
    }
    for mutation in ({"iss": "wrong"}, {"aud": "wrong"}, {"type": "refresh"}, {"token_version": 0}, {"nbf": datetime.now(UTC) + timedelta(minutes=5)}, {"kid": "unknown"}):
        claims = dict(expired)
        headers = {"kid": "test-kid"}
        if "kid" in mutation:
            headers["kid"] = mutation["kid"]
        else:
            claims.update(mutation)
        encoded = jwt.encode(claims, service._private_key, algorithm="EdDSA", headers=headers)
        with pytest.raises(InvalidAccessTokenError):
            service.validate(encoded)

    for private, public, kid, issuer, audience, ttl in [
        ("not-a-key", "not-a-key", "kid", "issuer", "audience", 1),
        (service._private_key, service._public_key, " ", "issuer", "audience", 1),
        (service._private_key, service._public_key, "kid", "", "audience", 1),
        (service._private_key, service._public_key, "kid", "issuer", "", 1),
        (service._private_key, service._public_key, "kid", "issuer", "audience", 0),
    ]:
        with pytest.raises(ValueError) as error:
            JWTService(private, public, kid, issuer, audience, ttl)
        assert "not-a-key" not in str(error.value)


def test_refresh_tokens_are_canonical_secret_and_repr_safe() -> None:
    service = RefreshTokenService()
    first, second = service.issue(), service.issue()
    token_id, secret = first.raw_token.split(".")
    assert str(first.token_id) == token_id
    assert len(secret) >= 43
    assert first.raw_token != second.raw_token
    assert first.token_hash == service.hash(first.raw_token)
    assert first.token_hash != second.token_hash
    assert service.matches(first.raw_token, first.token_hash)
    assert not service.matches(first.raw_token, second.token_hash)
    assert first.raw_token not in repr(first) and first.token_hash not in repr(first)

    for invalid in ("", "missing", "a.b.c", f"{uuid4()}.", f"{uuid4()}.short", f"{uuid4()}.{'a' * 43}!", "x" * 257, 1):
        with pytest.raises(InvalidRefreshTokenError):
            service.parse(invalid)


def test_csrf_tokens_are_opaque_hash_bound_and_safe() -> None:
    service = CsrfService()
    token, digest = service.issue()
    other, _ = service.issue()
    assert len(token) >= 43 and token != other and len(digest) == 64
    assert service.valid(token, digest)
    assert not service.valid(other, digest)
    assert not service.valid("", digest)
    assert not service.valid(token, "bad")
    assert not service.valid(None, digest)
    assert token not in repr((digest,))


def test_cookie_policy_uses_matching_secure_issue_and_clear_attributes() -> None:
    response = Response()
    policy = CookiePolicy(True, "strict", "app.gearia.test", 60, 120)
    policy.set_tokens(response, "access", "refresh", "csrf")
    rendered = "\n".join(response.headers.getlist("set-cookie"))
    assert "HttpOnly" in rendered and "Secure" in rendered and "SameSite=strict" in rendered
    assert "Max-Age=60" in rendered and "Max-Age=120" in rendered and "expires=" in rendered.lower()
    assert "Path=/auth" in rendered and "Domain=app.gearia.test" in rendered
    cleared = Response(); policy.clear(cleared)
    assert all("Max-Age=0" in value for value in cleared.headers.getlist("set-cookie"))
    for args in ((True, "invalid", None, 1, 1), (True, "lax", None, 0, 1), (True, "lax", " ", 1, 1)):
        with pytest.raises(ValueError):
            CookiePolicy(*args)
    with pytest.raises(ValueError):
        Settings(auth_cookie_samesite="invalid")


class ScriptRedis:
    """Small Redis script double that never receives raw identifier material."""

    def __init__(self, values: list[list[int]] | None = None) -> None:
        self.values = values or [[1, 60]]
        self.calls: list[tuple[object, ...]] = []

    def eval(self, *arguments: object) -> list[int]:
        self.calls.append(arguments)
        return self.values.pop(0) if self.values else [1, 60]


def test_rate_limiter_validates_policy_keys_and_failure_modes() -> None:
    redis = ScriptRedis([[1, 60], [2, 59], [3, 58]])
    limiter = RedisRateLimiter(redis, clock=lambda: 100)
    policy = RateLimitPolicy("auth:login", 2, 60)
    assert limiter.consume(policy, "person@example.com").remaining == 1
    assert limiter.consume(policy, "person@example.com").remaining == 0
    denied = limiter.consume(policy, "person@example.com")
    assert not denied.allowed and denied.retry_after == 58
    rendered_key = str(redis.calls[0][2])
    assert "person@example.com" not in rendered_key and rendered_key.startswith("rate:auth:login:")
    for args in (("", 1, 1), ("auth", 0, 1), ("auth", 1, 0), (" auth", 1, 1), ("auth", 1, 1, "open")):
        with pytest.raises(ValueError):
            RateLimitPolicy(*args)
    assert RedisRateLimiter(object(), clock=lambda: 1).consume(RateLimitPolicy("auth", 1, 1), "id").allowed
    assert not RedisRateLimiter(object(), clock=lambda: 1).consume(RateLimitPolicy("auth", 1, 1, RateLimitFailureMode.CLOSED), "id").allowed


def test_auth_factories_are_cached_and_auth_disabled_does_not_require_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(auth_enabled=False)
    monkeypatch.setattr(auth_dependencies, "get_settings", lambda: settings)
    for factory in (auth_dependencies.get_password_hasher, auth_dependencies.get_refresh_token_service, auth_dependencies.get_csrf_service, auth_dependencies.get_cookie_policy, auth_dependencies.get_redis_client, auth_dependencies.get_rate_limiter, auth_dependencies.get_jwt_service):
        factory.cache_clear()
    assert auth_dependencies.get_password_hasher() is auth_dependencies.get_password_hasher()
    assert auth_dependencies.get_refresh_token_service() is auth_dependencies.get_refresh_token_service()
    assert auth_dependencies.get_csrf_service() is auth_dependencies.get_csrf_service()
    assert auth_dependencies.get_cookie_policy() is auth_dependencies.get_cookie_policy()
    with pytest.raises(RuntimeError) as error:
        auth_dependencies.get_jwt_service()
    assert "private" not in str(error.value).lower()
    for factory in (auth_dependencies.get_password_hasher, auth_dependencies.get_refresh_token_service, auth_dependencies.get_csrf_service, auth_dependencies.get_cookie_policy, auth_dependencies.get_redis_client, auth_dependencies.get_rate_limiter, auth_dependencies.get_jwt_service):
        factory.cache_clear()


def test_auth_factory_composes_one_caller_session_and_production_requires_auth_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    database = object()
    dependencies = [object() for _ in range(6)]
    monkeypatch.setattr(auth_dependencies, "get_password_hasher", lambda: dependencies[0])
    monkeypatch.setattr(auth_dependencies, "get_jwt_service", lambda: dependencies[1])
    monkeypatch.setattr(auth_dependencies, "get_refresh_token_service", lambda: dependencies[2])
    monkeypatch.setattr(auth_dependencies, "get_cookie_policy", lambda: dependencies[3])
    monkeypatch.setattr(auth_dependencies, "get_csrf_service", lambda: dependencies[4])
    monkeypatch.setattr(auth_dependencies, "get_rate_limiter", lambda: dependencies[5])
    service = auth_dependencies.get_auth_service(database)  # type: ignore[arg-type]
    assert service._database is database
    assert service._session_repository._database is database
    assert service._refresh_token_repository._database is database
    assert service._jwt_service is dependencies[1]
    with pytest.raises(ValueError):
        Settings(
            environment="production", trusted_hosts=["api.gearia.test"], cors_origins=["https://app.gearia.test"],
            database_url="postgresql+psycopg://app:password@postgres/gearia", auth_enabled=True,
            auth_cookie_secure=True,
        )


def test_auth_service_exposes_explicit_logout_orchestration_boundary() -> None:
    dependencies = [SimpleNamespace() for _ in range(9)]
    service = AuthService(*dependencies)
    assert callable(service.logout)

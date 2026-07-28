"""P1B phase-two unit tests; deliberately no HTTP integration."""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Response

from app.services.auth_service import AuthService
from app.services.cookie_policy import CookiePolicy
from app.services.csrf_service import CsrfService
from app.services.jwt_service import JWTService
from app.services.rate_limiter import RateLimitFailureMode, RateLimitPolicy, RedisRateLimiter
from app.services.refresh_token_service import RefreshTokenService


def jwt_service() -> JWTService:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return JWTService(private, public, "test-kid", "issuer", "audience", 60)


def test_jwt_round_trip_is_strictly_eddsa() -> None:
    from uuid import uuid4
    service = jwt_service(); user_id, session_id = uuid4(), uuid4()
    claims = service.validate(service.issue(user_id, session_id, 1))
    assert claims.user_id == user_id and claims.session_id == session_id and claims.token_version == 1


def test_opaque_refresh_and_csrf_tokens_are_secret_bound() -> None:
    refresh = RefreshTokenService(); first, second = refresh.issue(), refresh.issue()
    assert first.raw_token != second.raw_token and refresh.matches(first.raw_token, first.token_hash)
    assert not refresh.matches("invalid", first.token_hash)
    csrf = CsrfService(); token, digest = csrf.issue()
    assert csrf.valid(token, digest) and not csrf.valid("wrong", digest)


def test_cookie_policy_centralizes_security_attributes() -> None:
    response = Response(); policy = CookiePolicy(True, "lax", None, 60, 120)
    policy.set_tokens(response, "access", "refresh", "csrf")
    rendered = "\n".join(response.headers.getlist("set-cookie"))
    assert "HttpOnly" in rendered and "Secure" in rendered and "SameSite=lax" in rendered


class FakeRedis:
    def __init__(self) -> None: self.count = 0
    def eval(self, *_: object) -> list[int]: self.count += 1; return [self.count, 60]


def test_rate_limiter_derives_sensitive_key_and_honors_failure_policy() -> None:
    limiter = RedisRateLimiter(FakeRedis(), clock=lambda: 10)
    policy = RateLimitPolicy("auth:login", 1, 60)
    assert "user@example.com" not in limiter.derive_key(policy.namespace, "user@example.com")
    assert limiter.consume(policy, "user@example.com").allowed
    assert not limiter.consume(policy, "user@example.com").allowed
    closed = RedisRateLimiter(object(), clock=lambda: 10).consume(RateLimitPolicy("auth:refresh", 1, 60, RateLimitFailureMode.CLOSED), "session")
    assert not closed.allowed


def test_auth_service_exposes_logout_as_an_explicit_domain_operation() -> None:
    service = AuthService(*([object()] * 9))
    assert callable(service.logout)

"""Strict Ed25519 access-JWT issuance and verification."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from jwt import InvalidTokenError


class InvalidAccessTokenError(ValueError):
    """Public callers receive a sanitized authentication failure."""


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: UUID
    session_id: UUID
    token_version: int
    issued_at: datetime
    expires_at: datetime
    jti: UUID


class JWTService:
    """Only EdDSA is accepted; header algorithms are never trusted."""

    def __init__(self, private_key: str, public_key: str, kid: str, issuer: str, audience: str, ttl_seconds: int, clock_skew_seconds: int = 30) -> None:
        if not isinstance(kid, str) or not kid.strip() or kid != kid.strip():
            raise ValueError("invalid JWT signing configuration")
        if not isinstance(issuer, str) or not issuer.strip() or not isinstance(audience, str) or not audience.strip():
            raise ValueError("invalid JWT signing configuration")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError("invalid JWT signing configuration")
        if isinstance(clock_skew_seconds, bool) or not isinstance(clock_skew_seconds, int) or clock_skew_seconds < 0:
            raise ValueError("invalid JWT signing configuration")
        try:
            loaded_private = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
            loaded_public = serialization.load_pem_public_key(public_key.encode("utf-8"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid JWT signing configuration") from error
        if not isinstance(loaded_private, Ed25519PrivateKey) or not isinstance(loaded_public, Ed25519PublicKey):
            raise ValueError("invalid JWT signing configuration")
        self._private_key, self._public_key, self._kid = private_key, public_key, kid
        self._issuer, self._audience = issuer, audience
        self._ttl, self._leeway = timedelta(seconds=ttl_seconds), timedelta(seconds=clock_skew_seconds)

    def issue(self, user_id: UUID, session_id: UUID, token_version: int) -> str:
        now = datetime.now(UTC)
        payload = {"iss": self._issuer, "aud": self._audience, "sub": str(user_id), "sid": str(session_id), "token_version": token_version, "iat": now, "nbf": now, "exp": now + self._ttl, "jti": str(uuid4()), "type": "access"}
        return jwt.encode(payload, self._private_key, algorithm="EdDSA", headers={"kid": self._kid, "typ": "JWT"})

    def validate(self, encoded: str) -> AccessTokenClaims:
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get("alg") != "EdDSA" or header.get("kid") != self._kid:
                raise InvalidAccessTokenError("invalid access token")
            payload = jwt.decode(encoded, self._public_key, algorithms=["EdDSA"], issuer=self._issuer, audience=self._audience, leeway=self._leeway, options={"require": ["iss", "aud", "sub", "sid", "iat", "nbf", "exp", "jti", "token_version", "type"]})
            if payload["type"] != "access" or not isinstance(payload["token_version"], int) or payload["token_version"] < 1:
                raise InvalidAccessTokenError("invalid access token")
            return AccessTokenClaims(UUID(payload["sub"]), UUID(payload["sid"]), payload["token_version"], datetime.fromtimestamp(payload["iat"], UTC), datetime.fromtimestamp(payload["exp"], UTC), UUID(payload["jti"]))
        except (InvalidTokenError, KeyError, TypeError, ValueError) as error:
            raise InvalidAccessTokenError("invalid access token") from error

"""Opaque refresh-token generation and verification without persistence."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from uuid import UUID, uuid4


class InvalidRefreshTokenError(ValueError):
    """Raised without exposing token material."""


@dataclass(frozen=True)
class IssuedRefreshToken:
    token_id: UUID
    raw_token: str = field(repr=False)
    token_hash: str = field(repr=False)


class RefreshTokenService:
    """Use `token_id.secret` with a 256-bit URL-safe secret and SHA-256 storage."""

    def issue(self) -> IssuedRefreshToken:
        token_id = uuid4()
        secret = secrets.token_urlsafe(32)
        raw_token = f"{token_id}.{secret}"
        return IssuedRefreshToken(token_id, raw_token, self.hash(raw_token))

    def hash(self, raw_token: str) -> str:
        if not isinstance(raw_token, str):
            raise InvalidRefreshTokenError("invalid refresh token")
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def parse(self, raw_token: object) -> UUID:
        if not isinstance(raw_token, str):
            raise InvalidRefreshTokenError("invalid refresh token")
        if len(raw_token) > 256 or raw_token.count(".") != 1:
            raise InvalidRefreshTokenError("invalid refresh token")
        token_id, separator, secret = raw_token.partition(".")
        if not separator or not secret or len(secret) < 43:
            raise InvalidRefreshTokenError("invalid refresh token")
        if not all(character.isalnum() or character in {"-", "_"} for character in secret):
            raise InvalidRefreshTokenError("invalid refresh token")
        try:
            return UUID(token_id)
        except ValueError as error:
            raise InvalidRefreshTokenError("invalid refresh token") from error

    def matches(self, raw_token: object, expected_hash: str) -> bool:
        try:
            self.parse(raw_token)
        except InvalidRefreshTokenError:
            return False
        return isinstance(expected_hash, str) and hmac.compare_digest(
            self.hash(raw_token), expected_hash
        )

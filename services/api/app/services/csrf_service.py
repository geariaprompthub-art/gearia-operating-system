"""Session-bound double-submit CSRF primitives."""

import hashlib
import hmac
import secrets


class CsrfService:
    """Generate opaque browser tokens and compare only their SHA-256 hashes."""

    def issue(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        return token, self.hash(token)

    def hash(self, token: str) -> str:
        if not isinstance(token, str):
            raise ValueError("invalid csrf token")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def valid(self, submitted: object, expected_hash: object) -> bool:
        return (
            isinstance(submitted, str)
            and bool(submitted)
            and len(submitted) <= 256
            and isinstance(expected_hash, str)
            and len(expected_hash) == 64
            and all(character in "0123456789abcdef" for character in expected_hash)
            and hmac.compare_digest(self.hash(submitted), expected_hash)
        )

    def valid_pair(self, cookie_value: object, header_value: object, expected_hash: object) -> bool:
        """Validate the bound double-submit pair without exposing either value."""

        return self.valid(cookie_value, expected_hash) and self.valid(header_value, expected_hash)

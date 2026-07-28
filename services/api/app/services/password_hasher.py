"""Argon2id password policy and hashing boundary."""

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class InvalidPasswordError(ValueError):
    """Raised without including password material in the message."""


class PasswordHashingService:
    """Use explicit production Argon2id parameters and a stable dummy verifier."""

    def __init__(self, time_cost: int = 3, memory_cost: int = 65536, parallelism: int = 2, hash_len: int = 32, salt_len: int = 16) -> None:
        self._hasher = PasswordHasher(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism, hash_len=hash_len, salt_len=salt_len, type=Type.ID)
        self._dummy = self._hasher.hash("gearia-dummy-password-not-a-user-secret")

    def validate(self, password: object) -> str:
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise InvalidPasswordError("password does not meet policy")
        return password

    def hash(self, password: object) -> str:
        return self._hasher.hash(self.validate(password))

    def verify(self, password: object, encoded: str) -> bool:
        try:
            return self._hasher.verify(encoded, self.validate(password))
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def verify_dummy(self, password: object) -> None:
        self.verify(password, self._dummy)

    def needs_rehash(self, encoded: str) -> bool:
        return self._hasher.check_needs_rehash(encoded)

"""PostgreSQL transaction-scoped coordination for idempotent registration."""

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session


class RegistrationCoordinationRepository:
    """Acquire an email-scoped advisory transaction lock without committing."""

    _NAMESPACE = "registration-email:"

    def __init__(self, database: Session) -> None:
        self._database = database

    @classmethod
    def advisory_key(cls, normalized_email: str) -> int:
        """Map a namespaced SHA-256 digest to PostgreSQL's signed bigint keyspace."""

        digest = hashlib.sha256(f"{cls._NAMESPACE}{normalized_email}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    def acquire_email_lock(self, normalized_email: str) -> None:
        """Hold `pg_advisory_xact_lock` until caller commit or rollback."""

        if not isinstance(normalized_email, str) or not normalized_email:
            raise ValueError("normalized email is required")
        self._database.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": self.advisory_key(normalized_email)})

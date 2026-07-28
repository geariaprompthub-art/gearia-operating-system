"""Internal identity operations; no sessions or tokens."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.user import User, UserStatus
from app.repositories.user_repository import EmailAlreadyExistsError, UserRepository
from app.schemas.user import CredentialResult, UserDTO
from app.services.email_normalization import normalize_email
from app.services.password_hasher import PasswordHashingService


class IdentityService:
    def __init__(self, database: Session, hasher: PasswordHashingService | None = None) -> None:
        self._repository = UserRepository(database)
        self._hasher = hasher or PasswordHashingService()

    @staticmethod
    def _dto(user: User) -> UserDTO:
        return UserDTO(id=user.id, email=user.email, status=user.status, email_verified_at=user.email_verified_at, token_version=user.token_version, created_at=user.created_at, updated_at=user.updated_at)

    def create_local_user(self, email: object, password: object) -> UserDTO:
        normalized = normalize_email(email)
        if self._repository.exists_by_normalized_email(normalized.normalized):
            raise EmailAlreadyExistsError("email already exists")
        user = User(email=normalized.email, email_normalized=normalized.normalized, password_hash=self._hasher.hash(password))
        return self._dto(self._repository.create(user))

    def verify_credentials(self, email: object, password: object) -> CredentialResult:
        normalized = normalize_email(email)
        user = self._repository.get_by_normalized_email(normalized.normalized)
        if user is None:
            self._hasher.verify_dummy(password)
            return CredentialResult(status="invalid_credentials")
        if not user.password_hash or not self._hasher.verify(password, user.password_hash):
            return CredentialResult(status="invalid_credentials")
        if user.status == UserStatus.PENDING_VERIFICATION:
            return CredentialResult(status="pending_verification", user=self._dto(user))
        if user.status == UserStatus.SUSPENDED:
            return CredentialResult(status="suspended")
        if user.status == UserStatus.ANONYMIZED:
            return CredentialResult(status="anonymized")
        if user.status == UserStatus.LOCKED:
            locked_until = user.locked_until
            if locked_until is not None and locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=UTC)
            if locked_until is not None and locked_until <= datetime.now(UTC):
                return CredentialResult(status="lock_expired")
            return CredentialResult(status="locked")
        return CredentialResult(status="valid", user=self._dto(user), rehash_required=self._hasher.needs_rehash(user.password_hash))

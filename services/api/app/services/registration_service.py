"""Atomic internal registration for P2B; no HTTP or email delivery."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import UserStatus
from app.repositories.user_repository import EmailAlreadyExistsError, UserRepository
from app.repositories.registration_coordination_repository import RegistrationCoordinationRepository
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.identity_service import IdentityService
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.personal_organization_provisioning_service import PersonalOrganizationProvisioningService


class RegistrationError(RuntimeError):
    """Sanitized internal registration failure."""


@dataclass(frozen=True)
class RegistrationResult:
    user_id: object
    workspace_id: object
    raw_verification_token: str = field(repr=False)
    verification_expires_at: object
    registration_state: str


class RegistrationService:
    """Own one commit for user, personal workspace, and verification challenge."""

    def __init__(self, database: Session, identity: IdentityService, provisioning: PersonalOrganizationProvisioningService, tokens: LifecycleTokenService, coordination: RegistrationCoordinationRepository | None = None, verification_ttl: timedelta = timedelta(hours=24)) -> None:
        self._database = database
        self._identity = identity
        self._provisioning = provisioning
        self._tokens = tokens
        self._coordination = coordination or RegistrationCoordinationRepository(database)
        self._ttl = verification_ttl

    def register(self, email: object, password: object) -> RegistrationResult:
        """Persist the whole aggregate or roll every staged mutation back."""

        try:
            from app.services.email_normalization import normalize_email
            canonical = normalize_email(email)
            self._coordination.acquire_email_lock(canonical.normalized)
            existing = self._identity.get_user_by_normalized_email(canonical.normalized)
            if existing is None:
                user = self._identity.create_local_user(email, password)
                persisted_user = UserRepository(self._database).get_by_id_for_update(user.id)
                if persisted_user is None:
                    raise RegistrationError("registration unavailable")
                provisioned = self._provisioning.provision_for_user(persisted_user)
                state = "created"
            elif existing.status == UserStatus.PENDING_VERIFICATION:
                persisted_user = UserRepository(self._database).get_by_id_for_update(existing.id)
                if persisted_user is None:
                    raise RegistrationError("registration unavailable")
                user = self._identity._dto(persisted_user)
                provisioned = self._provisioning.provision_for_user(persisted_user)
                state = "reissued"
            else:
                raise RegistrationError("registration unavailable")
            issued = self._tokens.issue_email_verification(user.id, self._ttl)
            self._database.commit()
            return RegistrationResult(user.id, provisioned.workspace_id, issued.raw_token, issued.expires_at, state)
        except (EmailAlreadyExistsError, IntegrityError) as error:
            self._database.rollback()
            raise RegistrationError("registration unavailable") from error
        except Exception:
            self._database.rollback()
            raise

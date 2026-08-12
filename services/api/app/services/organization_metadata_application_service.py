"""Internal transactional updates for the public organization display name only."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.organization import OrganizationStatus
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.organization_policy import OrganizationAction, OrganizationPolicy


class OrganizationMetadataError(RuntimeError):
    """Sanitized internal organization metadata failure."""


@dataclass(frozen=True)
class OrganizationMetadataResult:
    organization_id: UUID
    name: str
    slug: str
    kind: str
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationMetadataApplicationService:
    """Own one name-only transaction; slugs remain immutable in P3A."""

    def __init__(self, database: Session, organizations: OrganizationRepository, contexts: OrganizationContextResolver, policy: type[OrganizationPolicy] = OrganizationPolicy, structured_logger: SafeStructuredLogger | None = None) -> None:
        self._database, self._organizations, self._contexts, self._policy = database, organizations, contexts, policy
        self._logger = structured_logger

    @staticmethod
    def _name(value: object) -> str:
        if not isinstance(value, str):
            raise OrganizationMetadataError("organization metadata unavailable")
        normalized = value.strip()
        if not normalized or len(normalized) > 120:
            raise OrganizationMetadataError("organization metadata unavailable")
        return normalized

    def update_name(self, actor_user_id: UUID, organization_id: UUID, name: str) -> OrganizationMetadataResult:
        try:
            context = self._contexts.resolve(actor_user_id, organization_id)
            if not self._policy.is_allowed(context, OrganizationAction.UPDATE_ORGANIZATION):
                raise OrganizationMetadataError("organization metadata unavailable")
            normalized = self._name(name)
            organization = self._organizations.get_by_id_for_update(organization_id)
            if organization is None or organization.status != OrganizationStatus.ACTIVE.value:
                raise OrganizationMetadataError("organization metadata unavailable")
            if organization.name != normalized:
                self._organizations.update_name_and_slug(organization, normalized, organization.slug)
                self._database.commit()
                if self._logger:
                    self._logger.info(LogEvent.ORGANIZATION_UPDATED, "Organization updated", actor_user_id=str(actor_user_id), organization_id=str(organization.id), changed_fields=["name"], result="success")
            return OrganizationMetadataResult(organization.id, organization.name, organization.slug, organization.kind, organization.status, organization.created_at, organization.updated_at)
        except OrganizationContextUnavailableError as error:
            self._database.rollback()
            if self._logger:
                self._logger.warning(LogEvent.ORGANIZATION_OPERATION_DENIED, "Organization metadata operation denied", actor_user_id=str(actor_user_id), organization_id=str(organization_id), result="denied", reason_code="policy_denied")
            raise OrganizationMetadataError("organization metadata unavailable") from error
        except Exception:
            self._database.rollback()
            raise

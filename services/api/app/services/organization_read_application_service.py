"""ORM-free, authorization-scoped organization reads for the HTTP boundary."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.organization_policy import OrganizationAction, OrganizationPolicy


class OrganizationReadUnavailableError(RuntimeError):
    """Sanitized failure for unavailable or unauthorized organization reads."""


@dataclass(frozen=True)
class OrganizationRead:
    organization_id: UUID
    kind: str
    name: str
    slug: str
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationReadApplicationService:
    """Resolve active membership before projecting public-safe organization facts."""

    def __init__(self, organizations: OrganizationRepository, contexts: OrganizationContextResolver, policy: type[OrganizationPolicy] = OrganizationPolicy) -> None:
        self._organizations, self._contexts, self._policy = organizations, contexts, policy

    @staticmethod
    def _project(organization: Organization) -> OrganizationRead:
        return OrganizationRead(organization.id, organization.kind, organization.name, organization.slug, organization.status, organization.created_at, organization.updated_at)

    def list_accessible(self, actor_user_id: UUID) -> list[OrganizationRead]:
        if not isinstance(actor_user_id, UUID):
            raise OrganizationReadUnavailableError("organization unavailable")
        rows: list[OrganizationRead] = []
        for organization in self._organizations.list_accessible_by_user(actor_user_id):
            try:
                context = self._contexts.resolve(actor_user_id, organization.id)
                if self._policy.is_allowed(context, OrganizationAction.READ_ORGANIZATION):
                    rows.append(self._project(organization))
            except (OrganizationContextUnavailableError, ValueError):
                continue
        return rows

    def get_accessible(self, actor_user_id: UUID, organization_id: UUID) -> OrganizationRead:
        try:
            context = self._contexts.resolve(actor_user_id, organization_id)
            if not self._policy.is_allowed(context, OrganizationAction.READ_ORGANIZATION):
                raise OrganizationReadUnavailableError("organization unavailable")
            organization = self._organizations.get_by_id(organization_id)
            if organization is None:
                raise OrganizationReadUnavailableError("organization unavailable")
            return self._project(organization)
        except (OrganizationContextUnavailableError, TypeError, ValueError) as error:
            raise OrganizationReadUnavailableError("organization unavailable") from error

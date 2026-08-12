"""Read-only internal resolution of active organization authorization contexts."""

from uuid import UUID

from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMembership,
    OrganizationMembershipRole,
    OrganizationStatus,
)
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context import OrganizationContext


class OrganizationContextUnavailableError(RuntimeError):
    """Sanitized internal failure for absent, blocked, or inconsistent organization access."""


class OrganizationContextResolver:
    """Build immutable active contexts without committing, rolling back, or writing."""

    def __init__(
        self,
        organization_repository: OrganizationRepository,
        membership_repository: OrganizationMembershipRepository,
    ) -> None:
        self._organizations = organization_repository
        self._memberships = membership_repository

    @staticmethod
    def _build_context(
        *, user_id: UUID, organization: Organization, membership: OrganizationMembership
    ) -> OrganizationContext:
        try:
            kind = OrganizationKind(organization.kind)
            status = OrganizationStatus(organization.status)
            role = OrganizationMembershipRole(membership.role)
            return OrganizationContext(
                user_id=user_id,
                organization_id=organization.id,
                organization_kind=kind,
                organization_status=status,
                membership_id=membership.id,
                membership_role=role,
            )
        except (TypeError, ValueError) as error:
            raise OrganizationContextUnavailableError("organization access unavailable") from error

    def resolve(self, user_id: UUID, organization_id: UUID) -> OrganizationContext:
        organization = self._organizations.get_by_id(organization_id)
        if organization is None or organization.status != OrganizationStatus.ACTIVE.value:
            raise OrganizationContextUnavailableError("organization access unavailable")
        membership = self._memberships.get_active_for_user(organization_id, user_id)
        if (
            membership is None
            or membership.organization_id != organization_id
            or membership.user_id != user_id
            or membership.revoked_at is not None
        ):
            raise OrganizationContextUnavailableError("organization access unavailable")
        return self._build_context(user_id=user_id, organization=organization, membership=membership)

    def resolve_personal_for_user(self, user_id: UUID) -> OrganizationContext:
        organization = self._organizations.get_personal_by_owner_user_id(user_id)
        if (
            organization is None
            or organization.status != OrganizationStatus.ACTIVE.value
            or organization.personal_owner_user_id != user_id
        ):
            raise OrganizationContextUnavailableError("organization access unavailable")
        membership = self._memberships.get_active_for_user(organization.id, user_id)
        if (
            membership is None
            or membership.organization_id != organization.id
            or membership.user_id != user_id
            or membership.revoked_at is not None
            or membership.role != OrganizationMembershipRole.OWNER.value
        ):
            raise OrganizationContextUnavailableError("organization access unavailable")
        return self._build_context(user_id=user_id, organization=organization, membership=membership)

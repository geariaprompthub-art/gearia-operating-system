"""Authorized, ORM-free administrative invitation reads for the HTTP boundary."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.repositories.organization_invitation_repository import OrganizationInvitationRepository
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.organization_policy import OrganizationAction, OrganizationPolicy
from app.services.organization_invitation_application_service import OrganizationInvitationError


@dataclass(frozen=True)
class OrganizationInvitationRead:
    invitation_id: UUID
    organization_id: UUID
    role: str
    expires_at: datetime
    created_at: datetime


class OrganizationInvitationReadApplicationService:
    def __init__(self, invitations: OrganizationInvitationRepository, contexts: OrganizationContextResolver, policy: type[OrganizationPolicy] = OrganizationPolicy) -> None:
        self._invitations, self._contexts, self._policy = invitations, contexts, policy

    def list_active(self, actor_user_id: UUID, organization_id: UUID) -> tuple[OrganizationInvitationRead, ...]:
        try:
            context = self._contexts.resolve(actor_user_id, organization_id)
            if not self._policy.is_allowed(context, OrganizationAction.INVITE_MEMBER):
                raise OrganizationInvitationError("organization invitation unavailable")
            return tuple(
                OrganizationInvitationRead(item.id, item.organization_id, item.role, item.expires_at, item.created_at)
                for item in self._invitations.list_for_organization(organization_id)
                if item.accepted_at is None and item.invalidated_at is None
            )
        except (OrganizationContextUnavailableError, TypeError, ValueError) as error:
            raise OrganizationInvitationError("organization invitation unavailable") from error

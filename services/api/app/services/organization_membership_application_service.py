"""Internal transactional lifecycle for memberships of active shared organizations.

Lock order for every mutation is fixed: organization, actor membership, target
membership, active owner memberships, then owner recount.  The organization lock
serializes competing owner transitions before membership locks can invert.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.organization import (
    OrganizationKind,
    OrganizationMembership,
    OrganizationMembershipRole,
    OrganizationStatus,
)
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context import OrganizationContext
from app.services.organization_context_resolver import (
    OrganizationContextResolver,
    OrganizationContextUnavailableError,
)
from app.services.organization_policy import OrganizationAction, OrganizationPolicy


class OrganizationMembershipLifecycleError(RuntimeError):
    """Sanitized internal membership lifecycle failure."""


class LastOrganizationOwnerError(OrganizationMembershipLifecycleError):
    """The requested mutation would leave a shared organization without an owner."""


@dataclass(frozen=True)
class MembershipResult:
    """ORM-free representation of one active or historical membership."""

    membership_id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    created_at: datetime
    revoked_at: datetime | None


class OrganizationMembershipApplicationService:
    """Own commits and rollbacks for shared-membership mutations only."""

    def __init__(
        self,
        database: Session,
        organization_repository: OrganizationRepository,
        membership_repository: OrganizationMembershipRepository,
        context_resolver: OrganizationContextResolver,
        policy: type[OrganizationPolicy] = OrganizationPolicy,
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._database = database
        self._organizations = organization_repository
        self._memberships = membership_repository
        self._contexts = context_resolver
        self._policy = policy
        self._logger = structured_logger

    @staticmethod
    def _result(membership: OrganizationMembership) -> MembershipResult:
        return MembershipResult(
            membership_id=membership.id,
            organization_id=membership.organization_id,
            user_id=membership.user_id,
            role=membership.role,
            created_at=membership.created_at,
            revoked_at=membership.revoked_at,
        )

    def _locked_context(self, actor_user_id: UUID, organization_id: UUID) -> OrganizationContext:
        if not isinstance(actor_user_id, UUID) or not isinstance(organization_id, UUID):
            raise OrganizationMembershipLifecycleError("organization membership unavailable")
        organization = self._organizations.get_by_id_for_update(organization_id)
        if (
            organization is None
            or organization.kind != OrganizationKind.SHARED.value
            or organization.status != OrganizationStatus.ACTIVE.value
        ):
            raise OrganizationMembershipLifecycleError("organization membership unavailable")
        actor = self._memberships.get_active_for_user_for_update(organization_id, actor_user_id)
        if actor is None:
            raise OrganizationMembershipLifecycleError("organization membership unavailable")
        try:
            context = self._contexts.resolve(actor_user_id, organization_id)
        except OrganizationContextUnavailableError as error:
            raise OrganizationMembershipLifecycleError("organization membership unavailable") from error
        if context.membership_id != actor.id:
            raise OrganizationMembershipLifecycleError("organization membership unavailable")
        return context

    def _locked_target(self, organization_id: UUID, membership_id: UUID) -> OrganizationMembership:
        if not isinstance(membership_id, UUID):
            raise OrganizationMembershipLifecycleError("organization membership unavailable")
        target = self._memberships.get_by_id_in_organization_for_update(organization_id, membership_id)
        if target is None:
            raise OrganizationMembershipLifecycleError("organization membership unavailable")
        return target

    def _authorize(self, context: OrganizationContext, action: OrganizationAction) -> None:
        if not self._policy.is_allowed(context, action):
            raise OrganizationMembershipLifecycleError("organization membership unavailable")

    def _protect_last_owner(self, organization_id: UUID, target: OrganizationMembership) -> None:
        if target.role != OrganizationMembershipRole.OWNER.value:
            return
        self._memberships.list_active_owners_for_update(organization_id)
        if self._memberships.count_active_owners(organization_id) <= 1:
            raise LastOrganizationOwnerError("organization membership unavailable")

    @staticmethod
    def _role_action(current_role: str, new_role: str) -> OrganizationAction:
        if new_role == OrganizationMembershipRole.OWNER.value and current_role in {
            OrganizationMembershipRole.MEMBER.value,
            OrganizationMembershipRole.ADMIN.value,
        }:
            return OrganizationAction.PROMOTE_TO_OWNER
        if current_role == OrganizationMembershipRole.MEMBER.value and new_role == OrganizationMembershipRole.ADMIN.value:
            return OrganizationAction.CHANGE_MEMBER_TO_ADMIN
        if current_role == OrganizationMembershipRole.ADMIN.value and new_role == OrganizationMembershipRole.MEMBER.value:
            return OrganizationAction.CHANGE_ADMIN_TO_MEMBER
        if current_role == OrganizationMembershipRole.OWNER.value and new_role in {
            OrganizationMembershipRole.ADMIN.value,
            OrganizationMembershipRole.MEMBER.value,
        }:
            return OrganizationAction.DEMOTE_OWNER
        raise OrganizationMembershipLifecycleError("organization membership unavailable")

    def list_active(self, actor_user_id: UUID, organization_id: UUID) -> tuple[MembershipResult, ...]:
        """Return active memberships only after an active shared-membership authorization check."""

        context = self._locked_context(actor_user_id, organization_id)
        self._authorize(context, OrganizationAction.LIST_MEMBERSHIPS)
        return tuple(self._result(item) for item in self._memberships.list_active_for_organization(organization_id))

    def change_role(
        self,
        actor_user_id: UUID,
        organization_id: UUID,
        membership_id: UUID,
        new_role: str,
    ) -> MembershipResult:
        """Apply one allowed role transition with last-owner protection and one commit."""

        try:
            if not isinstance(new_role, str):
                raise OrganizationMembershipLifecycleError("organization membership unavailable")
            context = self._locked_context(actor_user_id, organization_id)
            target = self._locked_target(organization_id, membership_id)
            old_role = target.role
            action = self._role_action(target.role, new_role)
            self._authorize(context, action)
            self._protect_last_owner(organization_id, target)
            self._memberships.update_role(target, new_role)
            self._database.commit()
            if self._logger:
                self._logger.info(LogEvent.ORGANIZATION_MEMBERSHIP_ROLE_CHANGED, "Organization membership role changed", actor_user_id=str(actor_user_id), organization_id=str(organization_id), membership_id=str(target.id), old_role=old_role, new_role=new_role, result="success")
            return self._result(target)
        except OrganizationMembershipLifecycleError as error:
            self._database.rollback()
            if self._logger:
                self._logger.warning(LogEvent.ORGANIZATION_OPERATION_DENIED, "Organization membership operation denied", actor_user_id=str(actor_user_id), organization_id=str(organization_id), membership_id=str(membership_id), result="denied", reason_code="last_owner" if isinstance(error, LastOrganizationOwnerError) else "policy_denied")
            raise
        except Exception:
            self._database.rollback()
            raise

    def revoke(
        self, actor_user_id: UUID, organization_id: UUID, membership_id: UUID
    ) -> MembershipResult:
        """Revoke one active membership without physical deletion or silent idempotency."""

        try:
            context = self._locked_context(actor_user_id, organization_id)
            target = self._locked_target(organization_id, membership_id)
            if target.role == OrganizationMembershipRole.MEMBER.value:
                action = OrganizationAction.REMOVE_MEMBER
            elif target.role == OrganizationMembershipRole.ADMIN.value:
                action = OrganizationAction.REMOVE_ADMIN
            elif target.role == OrganizationMembershipRole.OWNER.value:
                action = OrganizationAction.REMOVE_OWNER
            else:
                raise OrganizationMembershipLifecycleError("organization membership unavailable")
            self._authorize(context, action)
            self._protect_last_owner(organization_id, target)
            self._memberships.revoke(target)
            self._database.commit()
            if self._logger:
                self._logger.info(LogEvent.ORGANIZATION_MEMBERSHIP_REVOKED, "Organization membership revoked", actor_user_id=str(actor_user_id), organization_id=str(organization_id), membership_id=str(target.id), result="success")
            return self._result(target)
        except OrganizationMembershipLifecycleError as error:
            self._database.rollback()
            if self._logger:
                self._logger.warning(LogEvent.ORGANIZATION_OPERATION_DENIED, "Organization membership operation denied", actor_user_id=str(actor_user_id), organization_id=str(organization_id), membership_id=str(membership_id), result="denied", reason_code="last_owner" if isinstance(error, LastOrganizationOwnerError) else "policy_denied")
            raise
        except Exception:
            self._database.rollback()
            raise

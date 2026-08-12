"""Fixed P3A role matrix, independent of HTTP and persistence."""

from enum import StrEnum

from app.models.organization import OrganizationMembershipRole
from app.services.organization_context import OrganizationContext


class OrganizationAction(StrEnum):
    READ_ORGANIZATION = "read_organization"
    UPDATE_ORGANIZATION = "update_organization"
    LIST_MEMBERSHIPS = "list_memberships"
    INVITE_MEMBER = "invite_member"
    INVALIDATE_INVITATION = "invalidate_invitation"
    REMOVE_MEMBER = "remove_member"
    REMOVE_ADMIN = "remove_admin"
    REMOVE_OWNER = "remove_owner"
    CHANGE_MEMBER_TO_ADMIN = "change_member_to_admin"
    CHANGE_ADMIN_TO_MEMBER = "change_admin_to_member"
    PROMOTE_TO_OWNER = "promote_to_owner"
    DEMOTE_OWNER = "demote_owner"
    CREATE_WORKSPACE = "create_workspace"
    UPDATE_WORKSPACE = "update_workspace"
    USE_WORKSPACE = "use_workspace"


class OrganizationPolicy:
    """Authorize only the fixed P3A matrix; services enforce target-state invariants."""

    _OWNER_ACTIONS = frozenset(OrganizationAction)
    _ADMIN_ACTIONS = frozenset(
        {
            OrganizationAction.READ_ORGANIZATION,
            OrganizationAction.UPDATE_ORGANIZATION,
            OrganizationAction.LIST_MEMBERSHIPS,
            OrganizationAction.INVITE_MEMBER,
            OrganizationAction.INVALIDATE_INVITATION,
            OrganizationAction.REMOVE_MEMBER,
            OrganizationAction.CREATE_WORKSPACE,
            OrganizationAction.UPDATE_WORKSPACE,
            OrganizationAction.USE_WORKSPACE,
        }
    )
    _MEMBER_ACTIONS = frozenset(
        {
            OrganizationAction.READ_ORGANIZATION,
            OrganizationAction.LIST_MEMBERSHIPS,
            OrganizationAction.USE_WORKSPACE,
        }
    )

    @classmethod
    def is_allowed(cls, context: OrganizationContext, action: OrganizationAction) -> bool:
        """Return a deterministic decision and fail closed for invalid inputs."""

        if not isinstance(context, OrganizationContext):
            raise ValueError("organization context is required")
        if not isinstance(action, OrganizationAction):
            raise ValueError("organization action is invalid")
        if not isinstance(context.membership_role, OrganizationMembershipRole):
            raise ValueError("organization membership role is invalid")
        if context.membership_role is OrganizationMembershipRole.OWNER:
            return action in cls._OWNER_ACTIONS
        if context.membership_role is OrganizationMembershipRole.ADMIN:
            return action in cls._ADMIN_ACTIONS
        if context.membership_role is OrganizationMembershipRole.MEMBER:
            return action in cls._MEMBER_ACTIONS
        raise ValueError("organization membership role is invalid")

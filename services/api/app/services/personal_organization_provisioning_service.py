"""Transaction-neutral provisioning of the P3A personal organization graph."""

from dataclasses import dataclass
from uuid import UUID

from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMembership,
    OrganizationMembershipRole,
    OrganizationStatus,
)
from app.models.user import User, UserStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.workspace_repository import WorkspaceRepository


class PersonalOrganizationProvisioningError(RuntimeError):
    """Sanitized internal failure for an inconsistent personal ownership graph."""


@dataclass(frozen=True)
class PersonalOrganizationProvisioningResult:
    """ORM-free identifiers describing the staged personal aggregate."""

    user_id: UUID
    organization_id: UUID
    membership_id: UUID
    workspace_id: UUID
    created_organization: bool
    created_membership: bool
    created_workspace: bool


class PersonalOrganizationProvisioningService:
    """Stage a single personal organization graph; callers own transaction outcome."""

    _PERSONAL_NAME = "Personal workspace"

    def __init__(
        self,
        organization_repository: OrganizationRepository,
        membership_repository: OrganizationMembershipRepository,
        workspace_repository: WorkspaceRepository,
    ) -> None:
        self._organizations = organization_repository
        self._memberships = membership_repository
        self._workspaces = workspace_repository

    @classmethod
    def personal_slug(cls, user_id: UUID) -> str:
        """Return the deterministic, non-PII slug shared with migration backfill."""

        return f"personal-{user_id.hex}"

    def provision_for_user(self, user: User) -> PersonalOrganizationProvisioningResult:
        """Create or reuse the personal graph without commit, rollback, or I/O."""

        if not isinstance(user, User):
            raise PersonalOrganizationProvisioningError("personal organization unavailable")
        organization = self._organizations.get_personal_by_owner_user_id_for_update(user.id)
        created_organization = organization is None
        if organization is None:
            organization = self._organizations.create(
                Organization(
                    kind=OrganizationKind.PERSONAL.value,
                    name=self._PERSONAL_NAME,
                    slug=self.personal_slug(user.id),
                    status=OrganizationStatus.ACTIVE.value,
                    personal_owner_user_id=user.id,
                )
            )

        membership = self._memberships.get_active_for_user_for_update(organization.id, user.id)
        created_membership = membership is None
        if membership is None:
            membership = self._memberships.create(
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=user.id,
                    role=OrganizationMembershipRole.OWNER.value,
                )
            )

        workspace = self._workspaces.get_by_owner_user_id_for_update(user.id)
        created_workspace = workspace is None
        if workspace is None:
            workspace = self._workspaces.create(
                Workspace(
                    owner_user_id=user.id,
                    organization_id=organization.id,
                    name=self._PERSONAL_NAME,
                    status=WorkspaceStatus.ACTIVE.value,
                )
            )
        elif workspace.organization_id is None:
            self._workspaces.set_organization(workspace, organization.id)

        self._validate(user, organization, membership, workspace)
        return PersonalOrganizationProvisioningResult(
            user_id=user.id,
            organization_id=organization.id,
            membership_id=membership.id,
            workspace_id=workspace.id,
            created_organization=created_organization,
            created_membership=created_membership,
            created_workspace=created_workspace,
        )

    @staticmethod
    def _validate(
        user: User,
        organization: Organization,
        membership: OrganizationMembership,
        workspace: Workspace,
    ) -> None:
        if (
            organization.kind != OrganizationKind.PERSONAL.value
            or organization.status != OrganizationStatus.ACTIVE.value
            or organization.personal_owner_user_id != user.id
            or membership.organization_id != organization.id
            or membership.user_id != user.id
            or membership.role != OrganizationMembershipRole.OWNER.value
            or membership.revoked_at is not None
            or workspace.owner_user_id != user.id
            or workspace.organization_id != organization.id
            or workspace.status != WorkspaceStatus.ACTIVE.value
            or user.status == UserStatus.ANONYMIZED.value
        ):
            raise PersonalOrganizationProvisioningError("personal organization unavailable")

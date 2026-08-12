"""Read-only resolution of operational workspace contexts through organization membership."""

from uuid import UUID

from app.models.workspace import WorkspaceStatus
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.workspace_context import WorkspaceContext


class WorkspaceContextUnavailableError(RuntimeError):
    """Workspace access is absent, blocked, revoked, or cross-organization."""


class WorkspaceContextResolver:
    """Build immutable contexts without owner-user authorization bypasses."""

    def __init__(self, workspaces: WorkspaceRepository, organizations: OrganizationContextResolver) -> None:
        self._workspaces = workspaces
        self._organizations = organizations

    def resolve(self, user_id: UUID, workspace_id: UUID) -> WorkspaceContext:
        workspace = self._workspaces.get_by_id(workspace_id)
        if workspace is None or workspace.status != WorkspaceStatus.ACTIVE:
            raise WorkspaceContextUnavailableError("workspace unavailable")
        try:
            organization = self._organizations.resolve(user_id, workspace.organization_id)
        except OrganizationContextUnavailableError as error:
            raise WorkspaceContextUnavailableError("workspace unavailable") from error
        return WorkspaceContext(workspace.id, user_id, organization.organization_id, organization.organization_kind.value, organization.membership_id, organization.membership_role.value)

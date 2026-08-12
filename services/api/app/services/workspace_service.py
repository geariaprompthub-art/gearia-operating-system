"""Application services for P2A workspace ownership and canonical source links."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.source import Source
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.personal_organization_provisioning_service import (
    PersonalOrganizationProvisioningService,
)
from app.repositories.workspace_source_repository import WorkspaceSourceRepository
from app.services.workspace_context import WorkspaceContext
from app.services.workspace_visibility_projection_service import WorkspaceVisibilityProjectionService


class WorkspaceNotFoundError(RuntimeError):
    """The authenticated user has no provisioned personal workspace."""


class WorkspaceBlockedError(RuntimeError):
    """A retained workspace cannot be used after owner anonymization."""


class SourceNotFoundError(RuntimeError):
    """A requested canonical source does not exist."""


class WorkspaceService:
    """Own personal-workspace lifecycle without impersonating future membership models."""

    def __init__(self, database: Session, repository: WorkspaceRepository | None = None) -> None:
        self._database = database
        self._repository = repository or WorkspaceRepository(database)

    def provision_personal_workspace(self, user_id: UUID, name: str = "Personal workspace") -> Workspace:
        """Stage the single personal ownership graph; caller owns transaction."""

        if self._repository.get_by_owner_user_id(user_id) is not None:
            raise ValueError("personal workspace already exists")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("workspace name must not be empty")
        user = self._database.get(User, user_id)
        if user is None:
            raise WorkspaceNotFoundError("workspace owner not found")
        provisioner = PersonalOrganizationProvisioningService(
            organization_repository=OrganizationRepository(self._database),
            membership_repository=OrganizationMembershipRepository(self._database),
            workspace_repository=self._repository,
        )
        result = provisioner.provision_for_user(user)
        workspace = self._repository.get_by_id(result.workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError("workspace not found")
        workspace.name = normalized_name
        self._database.flush()
        return workspace

    def get_or_provision_personal_workspace(self, user_id: UUID) -> Workspace:
        """Return the unique personal workspace or stage it in the caller transaction."""

        return self._repository.get_by_owner_user_id(user_id) or self.provision_personal_workspace(user_id)

    def get_current(self, context: WorkspaceContext) -> Workspace:
        """Resolve the aggregate root only when the context owner matches its workspace."""

        workspace = self._repository.get_by_id(context.workspace_id)
        if workspace is None or (context.organization_id is not None and workspace.organization_id != context.organization_id):
            raise WorkspaceNotFoundError("workspace not found")
        if workspace.status != WorkspaceStatus.ACTIVE:
            raise WorkspaceBlockedError("workspace is unavailable")
        return workspace

    def rename_current(self, context: WorkspaceContext, name: str) -> Workspace:
        """Rename only the aggregate root asserted by the current context."""

        workspace = self.get_current(context)
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("workspace name must not be empty")
        workspace.name = normalized_name
        self._database.flush()
        return workspace


class WorkspaceSourceService:
    """Link canonical sources and synchronously rebuild the affected derived projection."""

    def __init__(
        self,
        database: Session,
        source_repository: WorkspaceSourceRepository,
        projection_service: WorkspaceVisibilityProjectionService,
    ) -> None:
        self._database = database
        self._source_repository = source_repository
        self._projection_service = projection_service

    def link(self, context: WorkspaceContext, source_id: UUID) -> bool:
        """Link a canonical source once and rebuild only the current workspace projection."""

        if self._database.get(Source, source_id) is None:
            raise SourceNotFoundError("source not found")
        if self._source_repository.get_link(context, source_id) is not None:
            return False
        self._source_repository.create(context, source_id)
        self._projection_service.rebuild(context)
        return True

    def unlink(self, context: WorkspaceContext, source_id: UUID) -> bool:
        """Remove one link and rebuild without changing the canonical source or content."""

        removed = self._source_repository.delete(context, source_id)
        if removed:
            self._projection_service.rebuild(context)
        return removed

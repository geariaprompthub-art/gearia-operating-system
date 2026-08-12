"""Organization-scoped workspace reads and shared-workspace creation."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.organization_policy import OrganizationAction, OrganizationPolicy


class OrganizationWorkspaceError(RuntimeError):
    """Organization workspace access is unavailable or forbidden."""


@dataclass(frozen=True)
class OrganizationWorkspaceRead:
    workspace_id: UUID
    organization_id: UUID
    name: str
    status: str


class OrganizationWorkspaceApplicationService:
    """Own shared-workspace writes; reads remain ORM-free projections."""

    def __init__(self, database: Session, workspaces: WorkspaceRepository, contexts: OrganizationContextResolver, structured_logger: SafeStructuredLogger | None = None) -> None:
        self._database = database
        self._workspaces = workspaces
        self._contexts = contexts
        self._logger = structured_logger

    def _authorize(self, actor_user_id: UUID, organization_id: UUID, action: OrganizationAction):
        try:
            context = self._contexts.resolve(actor_user_id, organization_id)
            if not OrganizationPolicy.is_allowed(context, action):
                raise OrganizationWorkspaceError("workspace access denied")
            return context
        except (OrganizationContextUnavailableError, TypeError, ValueError) as error:
            raise OrganizationWorkspaceError("workspace access denied") from error

    @staticmethod
    def _read(workspace: Workspace) -> OrganizationWorkspaceRead:
        return OrganizationWorkspaceRead(workspace.id, workspace.organization_id, workspace.name, workspace.status)

    def list_accessible(self, actor_user_id: UUID, organization_id: UUID) -> tuple[OrganizationWorkspaceRead, ...]:
        self._authorize(actor_user_id, organization_id, OrganizationAction.USE_WORKSPACE)
        return tuple(self._read(item) for item in self._workspaces.list_by_organization_id(organization_id) if item.status == WorkspaceStatus.ACTIVE)

    def get_accessible(self, actor_user_id: UUID, organization_id: UUID, workspace_id: UUID) -> OrganizationWorkspaceRead:
        self._authorize(actor_user_id, organization_id, OrganizationAction.USE_WORKSPACE)
        workspace = self._workspaces.get_by_organization_id_and_id(organization_id, workspace_id)
        if workspace is None or workspace.status != WorkspaceStatus.ACTIVE:
            raise OrganizationWorkspaceError("workspace access denied")
        return self._read(workspace)

    def create_shared(self, actor_user_id: UUID, organization_id: UUID, name: str) -> OrganizationWorkspaceRead:
        context = self._authorize(actor_user_id, organization_id, OrganizationAction.CREATE_WORKSPACE)
        normalized_name = name.strip()
        if context.organization_kind.value != "shared" or not normalized_name:
            raise OrganizationWorkspaceError("workspace creation denied")
        workspace = self._workspaces.create(Workspace(organization_id=context.organization_id, owner_user_id=None, name=normalized_name))
        self._database.commit()
        self._database.refresh(workspace)
        if self._logger:
            self._logger.info(LogEvent.ORGANIZATION_WORKSPACE_CREATED, "Organization workspace created", actor_user_id=str(actor_user_id), organization_id=str(context.organization_id), workspace_id=str(workspace.id), result="success")
        return self._read(workspace)

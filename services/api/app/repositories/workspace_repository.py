"""Persistence boundary for the Workspace aggregate root."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workspace import Workspace


class WorkspaceRepository:
    """Access workspaces without committing a caller-owned transaction."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, workspace: Workspace) -> Workspace:
        """Stage a new aggregate root and allocate its identifier on flush."""

        self._database.add(workspace)
        self._database.flush()
        return workspace

    def get_by_id(self, workspace_id: UUID) -> Workspace | None:
        """Return one aggregate root by its stable identifier."""

        return self._database.get(Workspace, workspace_id)

    def get_by_organization_id_and_id(self, organization_id: UUID, workspace_id: UUID) -> Workspace | None:
        """Return a workspace only when both tenant identifiers agree."""

        return self._database.scalar(
            select(Workspace).where(Workspace.id == workspace_id, Workspace.organization_id == organization_id)
        )

    def get_by_owner_user_id(self, user_id: UUID) -> Workspace | None:
        """Return the P2A personal workspace for one owner, if provisioned."""

        return self._database.scalar(select(Workspace).where(Workspace.owner_user_id == user_id))

    def get_by_owner_user_id_for_update(self, user_id: UUID) -> Workspace | None:
        """Lock the owner's single personal aggregate without committing."""

        return self._database.scalar(
            select(Workspace).where(Workspace.owner_user_id == user_id).with_for_update()
        )

    def list_by_organization_id(self, organization_id: UUID) -> list[Workspace]:
        """Return organization workspaces in stable creation order without authorizing access."""

        return list(
            self._database.scalars(
                select(Workspace)
                .where(Workspace.organization_id == organization_id)
                .order_by(Workspace.created_at.asc(), Workspace.id.asc())
            )
        )

    def set_organization(self, workspace: Workspace, organization_id: UUID) -> None:
        """Associate a legacy personal workspace with its consistent organization."""

        workspace.organization_id = organization_id
        self._database.flush()

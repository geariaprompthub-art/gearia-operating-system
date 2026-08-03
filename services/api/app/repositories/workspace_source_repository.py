"""Workspace-scoped access to links with canonical sources."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.source import Source
from app.models.workspace_source import WorkspaceSource
from app.services.workspace_context import WorkspaceContext


class WorkspaceContextRequiredError(ValueError):
    """Raised before any private-resource query can run without tenant scope."""


def require_workspace_context(context: WorkspaceContext) -> WorkspaceContext:
    """Guard every private repository boundary consistently."""

    if not isinstance(context, WorkspaceContext):
        raise WorkspaceContextRequiredError("workspace context is required")
    return context


class WorkspaceSourceRepository:
    """Read and stage source links only within an explicit workspace boundary."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def get_link(self, context: WorkspaceContext, source_id: UUID) -> WorkspaceSource | None:
        """Read one source link while applying workspace scope in SQL."""

        context = require_workspace_context(context)
        return self._database.scalar(
            select(WorkspaceSource).where(
                WorkspaceSource.workspace_id == context.workspace_id,
                WorkspaceSource.source_id == source_id,
            )
        )

    def list_sources(self, context: WorkspaceContext) -> list[Source]:
        """List only canonical sources explicitly linked to this workspace."""

        context = require_workspace_context(context)
        statement = (
            select(Source)
            .join(WorkspaceSource, WorkspaceSource.source_id == Source.id)
            .where(WorkspaceSource.workspace_id == context.workspace_id)
            .order_by(Source.created_at.asc(), Source.id.asc())
        )
        return list(self._database.scalars(statement))

    def list_source_ids(self, context: WorkspaceContext) -> list[UUID]:
        """Return the canonical source IDs that define the visibility projection."""

        context = require_workspace_context(context)
        return list(
            self._database.scalars(
                select(WorkspaceSource.source_id)
                .where(WorkspaceSource.workspace_id == context.workspace_id)
                .order_by(WorkspaceSource.created_at.asc(), WorkspaceSource.source_id.asc())
            )
        )

    def create(self, context: WorkspaceContext, source_id: UUID) -> WorkspaceSource:
        """Stage one workspace-to-canonical-source link without committing."""

        context = require_workspace_context(context)
        link = WorkspaceSource(workspace_id=context.workspace_id, source_id=source_id)
        self._database.add(link)
        self._database.flush()
        return link

    def delete(self, context: WorkspaceContext, source_id: UUID) -> bool:
        """Delete one scoped link; never affect a link in another workspace."""

        link = self.get_link(context, source_id)
        if link is None:
            return False
        self._database.delete(link)
        self._database.flush()
        return True

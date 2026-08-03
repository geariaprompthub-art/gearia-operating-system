"""Build and verify the derived workspace content-visibility projection."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.workspace_source import WorkspaceSource
from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.services.workspace_context import WorkspaceContext, WorkspaceExecutionContext


@dataclass(frozen=True)
class WorkspaceProjectionConsistency:
    """Result of comparing primary links with the rebuildable visibility projection."""

    expected_content_ids: tuple[str, ...]
    projected_content_ids: tuple[str, ...]

    @property
    def consistent(self) -> bool:
        """Whether the projection exactly represents canonical content of linked sources."""

        return self.expected_content_ids == self.projected_content_ids


class WorkspaceVisibilityProjectionService:
    """Rebuild a workspace projection from primary source links and canonical content."""

    def __init__(self, database: Session, repository: WorkspaceContentVisibilityRepository) -> None:
        self._database = database
        self._repository = repository

    def expected_content_ids(self, context: WorkspaceContext) -> list:
        """Read the authoritative content set derived from workspace source links."""

        statement = (
            select(Content.id)
            .join(WorkspaceSource, WorkspaceSource.source_id == Content.source_id)
            .where(WorkspaceSource.workspace_id == context.workspace_id)
            .order_by(Content.id.asc())
        )
        return list(self._database.scalars(statement))

    def rebuild(
        self,
        context: WorkspaceContext,
        execution_context: WorkspaceExecutionContext | None = None,
    ) -> int:
        """Replace the derived rows from source links; optional job context must match scope."""

        if execution_context is not None and execution_context.workspace_id != context.workspace_id:
            raise ValueError("execution context must match workspace context")
        expected = self.expected_content_ids(context)
        self._repository.replace(context, expected)
        return len(expected)

    def verify(self, context: WorkspaceContext) -> WorkspaceProjectionConsistency:
        """Compare source-of-truth links with stored projection without mutating either."""

        expected = tuple(str(value) for value in self.expected_content_ids(context))
        projected = tuple(str(value) for value in self._repository.list_content_ids(context))
        return WorkspaceProjectionConsistency(expected, projected)

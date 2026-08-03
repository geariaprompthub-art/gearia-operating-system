"""Persistence boundary for the rebuildable content-visibility projection."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.workspace_content_visibility import WorkspaceContentVisibility
from app.models.content import Content
from app.repositories.workspace_source_repository import require_workspace_context
from app.services.workspace_context import WorkspaceContext


class WorkspaceContentVisibilityRepository:
    """Maintain derived workspace visibility without owning canonical content."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def list_content_ids(self, context: WorkspaceContext) -> list[UUID]:
        """Read workspace-visible content IDs in deterministic identifier order."""

        context = require_workspace_context(context)
        return list(
            self._database.scalars(
                select(WorkspaceContentVisibility.content_id)
                .where(WorkspaceContentVisibility.workspace_id == context.workspace_id)
                .order_by(WorkspaceContentVisibility.content_id.asc())
            )
        )

    def filter_content_ids(self, context: WorkspaceContext, content_ids: Sequence[UUID]) -> list[UUID]:
        """Filter an ordered candidate sequence in SQL while preserving caller order."""

        context = require_workspace_context(context)
        if any(not isinstance(content_id, UUID) for content_id in content_ids):
            raise ValueError("content_ids must contain UUID values")
        if len(set(content_ids)) != len(content_ids):
            raise ValueError("content_ids must be unique")
        if not content_ids:
            return []
        visible = set(
            self._database.scalars(
                select(WorkspaceContentVisibility.content_id).where(
                    WorkspaceContentVisibility.workspace_id == context.workspace_id,
                    WorkspaceContentVisibility.content_id.in_(content_ids),
                )
            )
        )
        return [content_id for content_id in content_ids if content_id in visible]

    def list_contents(self, context: WorkspaceContext) -> list[Content]:
        """Hydrate only the current workspace projection in stable canonical-content order."""

        context = require_workspace_context(context)
        return list(
            self._database.scalars(
                select(Content)
                .join(
                    WorkspaceContentVisibility,
                    WorkspaceContentVisibility.content_id == Content.id,
                )
                .where(WorkspaceContentVisibility.workspace_id == context.workspace_id)
                .order_by(Content.created_at.desc(), Content.id.asc())
            )
        )

    def replace(self, context: WorkspaceContext, content_ids: Sequence[UUID]) -> None:
        """Replace only this workspace's derived rows; the caller owns commit/rollback."""

        context = require_workspace_context(context)
        if any(not isinstance(content_id, UUID) for content_id in content_ids):
            raise ValueError("content_ids must contain UUID values")
        if len(set(content_ids)) != len(content_ids):
            raise ValueError("content_ids must be unique")
        self._database.execute(
            delete(WorkspaceContentVisibility).where(
                WorkspaceContentVisibility.workspace_id == context.workspace_id
            )
        )
        self._database.add_all(
            [
                WorkspaceContentVisibility(
                    workspace_id=context.workspace_id,
                    content_id=content_id,
                )
                for content_id in content_ids
            ]
        )
        self._database.flush()

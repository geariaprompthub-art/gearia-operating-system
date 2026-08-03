"""Workspace-scoped boundary over the unchanged hybrid retrieval pipeline."""

from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.services.hybrid_search_service import HybridSearchService
from app.services.workspace_context import WorkspaceContext


class WorkspaceHybridSearchService:
    """Resolve visibility before any retrieval candidate query is issued."""

    def __init__(
        self,
        hybrid_search: HybridSearchService,
        visibility_repository: WorkspaceContentVisibilityRepository,
    ) -> None:
        self._hybrid_search = hybrid_search
        self._visibility_repository = visibility_repository

    def search(self, context: WorkspaceContext, query: str, top_k: int) -> dict[str, object]:
        """Search only the rebuildable projection selected by the mandatory workspace context."""

        visible_content_ids = self._visibility_repository.list_content_ids(context)
        if not visible_content_ids:
            return {"items": [], "total": 0}
        return self._hybrid_search.search(query, top_k, visible_content_ids)

"""Tenant boundary tests for workspace-scoped hybrid retrieval."""

from uuid import UUID, uuid4

from app.db import SessionLocal
from app.models.content import Content
from app.models.source import Source
from app.models.user import User
from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.services.workspace_context import WorkspaceContext
from app.services.workspace_hybrid_search_service import WorkspaceHybridSearchService
from app.services.workspace_service import WorkspaceService
from app.services.workspace_visibility_projection_service import WorkspaceVisibilityProjectionService
from app.repositories.workspace_source_repository import WorkspaceSourceRepository


class RecordingHybridSearch:
    """Test double that records the immutable candidate universe passed to retrieval."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[UUID]]] = []

    def search(self, query: str, top_k: int, visible_content_ids: list[UUID]) -> dict[str, object]:
        self.calls.append((query, top_k, list(visible_content_ids)))
        return {"items": [], "total": 0}


def test_workspace_hybrid_search_selects_visibility_before_retrieval_and_returns_empty_without_provider() -> None:
    """No global candidate universe reaches hybrid retrieval when a workspace has no visibility rows."""

    database = SessionLocal()
    marker = uuid4().hex
    try:
        user = User(email=f"p2a-search-{marker}@test.local", email_normalized=f"p2a-search-{marker}@test.local", password_hash="hash")
        source = Source(name=f"p2a-search-source-{marker}", type="manual")
        database.add_all([user, source])
        database.flush()
        visible = Content(source_id=source.id, title="Visible", url=f"https://test/{marker}/visible", fingerprint=f"p2a-search-visible-{marker}", processing_status="processed")
        database.add(visible)
        database.flush()
        workspace = WorkspaceService(database).provision_personal_workspace(user.id)
        context = WorkspaceContext(workspace.id, user.id)
        visibility = WorkspaceContentVisibilityRepository(database)
        projection = WorkspaceVisibilityProjectionService(database, visibility)
        WorkspaceSourceRepository(database).create(context, source.id)
        projection.rebuild(context)

        hybrid = RecordingHybridSearch()
        service = WorkspaceHybridSearchService(hybrid, visibility)  # type: ignore[arg-type]
        assert service.search(context, "query", 10) == {"items": [], "total": 0}
        assert hybrid.calls == [("query", 10, [visible.id])]

        other_user = User(email=f"p2a-search-other-{marker}@test.local", email_normalized=f"p2a-search-other-{marker}@test.local", password_hash="hash")
        database.add(other_user)
        database.flush()
        other_workspace = WorkspaceService(database).provision_personal_workspace(other_user.id)
        empty_context = WorkspaceContext(other_workspace.id, other_user.id)
        assert service.search(empty_context, "query", 10) == {"items": [], "total": 0}
        assert len(hybrid.calls) == 1
    finally:
        database.rollback()
        database.close()

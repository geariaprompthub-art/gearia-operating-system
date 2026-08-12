"""PostgreSQL proof that shared workspace data remains workspace-isolated."""

from uuid import UUID, uuid4

from app.db import SessionLocal
from app.models.content import Content
from app.models.organization import Organization, OrganizationMembership
from app.models.source import Source
from app.models.user import User
from app.models.workspace import Workspace
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.repositories.workspace_source_repository import WorkspaceSourceRepository
from app.services.organization_context_resolver import OrganizationContextResolver
from app.services.workspace_context_resolver import WorkspaceContextResolver, WorkspaceContextUnavailableError
from app.services.workspace_hybrid_search_service import WorkspaceHybridSearchService
from app.services.workspace_visibility_projection_service import WorkspaceVisibilityProjectionService


class RecordingSearch:
    def __init__(self) -> None:
        self.calls: list[list[UUID]] = []

    def search(self, _query: str, _top_k: int, ids: list[UUID]) -> dict[str, object]:
        self.calls.append(list(ids))
        return {"items": [], "total": 0}


def test_same_shared_organization_cannot_cross_workspace_visibility_or_search() -> None:
    database = SessionLocal()
    try:
        marker = uuid4().hex
        user = User(email=f"p3a-shared-{marker}@test.local", email_normalized=f"p3a-shared-{marker}@test.local", password_hash="hash", status="active")
        organization = Organization(kind="shared", name="Shared", slug=f"p3a-shared-{marker}", status="active", personal_owner_user_id=None)
        first_source = Source(name=f"p3a-alpha-{marker}", type="manual")
        second_source = Source(name=f"p3a-beta-{marker}", type="manual")
        database.add_all([user, organization, first_source, second_source]); database.flush()
        database.add(OrganizationMembership(organization_id=organization.id, user_id=user.id, role="member"))
        first_workspace = Workspace(organization_id=organization.id, owner_user_id=None, name="A")
        second_workspace = Workspace(organization_id=organization.id, owner_user_id=None, name="B")
        first_content = Content(source_id=first_source.id, title="ALPHA_UNIQUE", url=f"https://test/{marker}/a", fingerprint=f"p3a-alpha-{marker}", processing_status="processed")
        second_content = Content(source_id=second_source.id, title="BETA_UNIQUE", url=f"https://test/{marker}/b", fingerprint=f"p3a-beta-{marker}", processing_status="processed")
        database.add_all([first_workspace, second_workspace, first_content, second_content]); database.flush()
        resolver = WorkspaceContextResolver(WorkspaceRepository(database), OrganizationContextResolver(OrganizationRepository(database), OrganizationMembershipRepository(database)))
        first_context, second_context = resolver.resolve(user.id, first_workspace.id), resolver.resolve(user.id, second_workspace.id)
        sources = WorkspaceSourceRepository(database); visibility = WorkspaceContentVisibilityRepository(database); projection = WorkspaceVisibilityProjectionService(database, visibility)
        sources.create(first_context, first_source.id); sources.create(second_context, second_source.id)
        projection.rebuild(first_context); projection.rebuild(second_context)
        assert visibility.filter_content_ids(first_context, [first_content.id, second_content.id]) == [first_content.id]
        assert visibility.filter_content_ids(second_context, [first_content.id, second_content.id]) == [second_content.id]
        search = RecordingSearch(); service = WorkspaceHybridSearchService(search, visibility)  # type: ignore[arg-type]
        service.search(first_context, "BETA_UNIQUE", 20); service.search(second_context, "ALPHA_UNIQUE", 20)
        assert search.calls == [[first_content.id], [second_content.id]]
        membership = OrganizationMembershipRepository(database).get_active_for_user(organization.id, user.id)
        assert membership is not None
        OrganizationMembershipRepository(database).revoke(membership); database.flush()
        try:
            resolver.resolve(user.id, first_workspace.id)
            assert False, "revoked membership must not resolve a workspace context"
        except WorkspaceContextUnavailableError:
            pass
    finally:
        database.rollback(); database.close()

"""FastAPI composition for explicit current-workspace boundaries."""

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.workspace_source_repository import WorkspaceSourceRepository
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.principal_dependencies import get_current_principal
from app.services.workspace_context import WorkspaceContext
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.workspace_context_resolver import WorkspaceContextResolver, WorkspaceContextUnavailableError
from app.models.workspace import WorkspaceStatus
from app.services.workspace_service import WorkspaceNotFoundError, WorkspaceService, WorkspaceSourceService
from app.services.workspace_visibility_projection_service import WorkspaceVisibilityProjectionService
from app.services.hybrid_search_dependencies import get_hybrid_search_service
from app.services.hybrid_search_service import HybridSearchService
from app.services.workspace_hybrid_search_service import WorkspaceHybridSearchService


def get_workspace_context(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    database: Session = Depends(get_db),
) -> WorkspaceContext:
    """Resolve only the principal's provisioned workspace; clients never choose its ID."""

    workspace = WorkspaceRepository(database).get_by_owner_user_id(principal.user_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    if workspace.status != WorkspaceStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace unavailable")
    organizations = OrganizationContextResolver(OrganizationRepository(database), OrganizationMembershipRepository(database))
    try:
        organization = organizations.resolve_personal_for_user(principal.user_id)
        context = WorkspaceContextResolver(WorkspaceRepository(database), organizations).resolve(principal.user_id, workspace.id)
    except (OrganizationContextUnavailableError, WorkspaceContextUnavailableError) as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace unavailable") from error
    if workspace.organization_id != organization.organization_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace unavailable")
    return context


def get_workspace_service(database: Session = Depends(get_db)) -> WorkspaceService:
    """Compose aggregate-root lifecycle operations for one request transaction."""

    return WorkspaceService(database)


def get_workspace_source_service(database: Session = Depends(get_db)) -> WorkspaceSourceService:
    """Compose source links and their rebuildable visibility projection."""

    visibility_repository = WorkspaceContentVisibilityRepository(database)
    return WorkspaceSourceService(
        database,
        WorkspaceSourceRepository(database),
        WorkspaceVisibilityProjectionService(database, visibility_repository),
    )


def get_workspace_hybrid_search_service(
    database: Session = Depends(get_db),
    hybrid_search: HybridSearchService = Depends(get_hybrid_search_service),
) -> WorkspaceHybridSearchService:
    """Wrap hybrid retrieval with projection-based visibility before candidate lookup."""

    return WorkspaceHybridSearchService(
        hybrid_search,
        WorkspaceContentVisibilityRepository(database),
    )

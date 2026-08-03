"""P2A current-workspace endpoints with dependency-derived tenant scope."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.repositories.workspace_source_repository import WorkspaceSourceRepository
from app.repositories.workspace_content_visibility_repository import WorkspaceContentVisibilityRepository
from app.schemas.content import ContentRead
from app.schemas.source import SourceRead
from app.schemas.workspace import WorkspaceRead, WorkspaceSourceCreate, WorkspaceUpdate
from app.schemas.hybrid_search import HybridSearchRequest, HybridSearchResponse
from app.services.workspace_context import WorkspaceContext
from app.services.workspace_dependencies import (
    get_workspace_context,
    get_workspace_service,
    get_workspace_source_service,
    get_workspace_hybrid_search_service,
)
from app.services.workspace_service import SourceNotFoundError, WorkspaceService, WorkspaceSourceService
from app.services.workspace_hybrid_search_service import WorkspaceHybridSearchService


router = APIRouter(prefix="/workspaces/current", tags=["workspaces"])


@router.get("", response_model=WorkspaceRead, summary="Get the current personal workspace")
def get_current_workspace(
    context: WorkspaceContext = Depends(get_workspace_context),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceRead:
    """Return only the aggregate root derived from the authenticated principal."""

    return WorkspaceRead.model_validate(service.get_current(context))


@router.patch("", response_model=WorkspaceRead, summary="Rename the current personal workspace")
def update_current_workspace(
    payload: WorkspaceUpdate,
    database: Session = Depends(get_db),
    context: WorkspaceContext = Depends(get_workspace_context),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceRead:
    """Rename the aggregate root in the request transaction only."""

    try:
        workspace = service.rename_current(context, payload.name)
        database.commit()
        database.refresh(workspace)
        return WorkspaceRead.model_validate(workspace)
    except ValueError as error:
        database.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid workspace") from error


@router.get("/sources", response_model=list[SourceRead], summary="List canonical sources linked to the current workspace")
def list_workspace_sources(
    context: WorkspaceContext = Depends(get_workspace_context),
    database: Session = Depends(get_db),
) -> list[SourceRead]:
    """Read source links through a repository that requires workspace scope."""

    return [SourceRead.model_validate(source) for source in WorkspaceSourceRepository(database).list_sources(context)]


@router.get("/contents", response_model=list[ContentRead], summary="List content visible in the current workspace")
def list_workspace_contents(
    context: WorkspaceContext = Depends(get_workspace_context),
    database: Session = Depends(get_db),
) -> list[ContentRead]:
    """Expose only the derived visibility projection, never global canonical content."""

    return [
        ContentRead.model_validate(content)
        for content in WorkspaceContentVisibilityRepository(database).list_contents(context)
    ]


@router.post(
    "/sources",
    response_model=list[SourceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Link a canonical source to the current workspace",
)
def link_workspace_source(
    payload: WorkspaceSourceCreate,
    database: Session = Depends(get_db),
    context: WorkspaceContext = Depends(get_workspace_context),
    service: WorkspaceSourceService = Depends(get_workspace_source_service),
) -> list[SourceRead]:
    """Create a scoped link then rebuild the derived visibility projection atomically."""

    try:
        service.link(context, payload.source_id)
        database.commit()
    except SourceNotFoundError as error:
        database.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found") from error
    except Exception:
        database.rollback()
        raise
    return [SourceRead.model_validate(source) for source in WorkspaceSourceRepository(database).list_sources(context)]


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Unlink a source from the current workspace")
def unlink_workspace_source(
    source_id: UUID,
    database: Session = Depends(get_db),
    context: WorkspaceContext = Depends(get_workspace_context),
    service: WorkspaceSourceService = Depends(get_workspace_source_service),
) -> Response:
    """Remove a link and rebuild only the caller workspace's projection."""

    try:
        if not service.unlink(context, source_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not linked to workspace")
        database.commit()
    except HTTPException:
        database.rollback()
        raise
    except Exception:
        database.rollback()
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/search/hybrid",
    response_model=HybridSearchResponse,
    summary="Search only content visible in the current workspace",
)
def search_workspace_hybrid(
    request: HybridSearchRequest,
    context: WorkspaceContext = Depends(get_workspace_context),
    service: WorkspaceHybridSearchService = Depends(get_workspace_hybrid_search_service),
) -> dict[str, object]:
    """Apply workspace visibility before lexical, vector, Graph, and reranking candidate stages."""

    return service.search(context, request.query, request.top_k)

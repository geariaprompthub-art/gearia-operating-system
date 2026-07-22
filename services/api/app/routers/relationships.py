"""HTTP endpoints for deterministic content relationships."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.relationship import (
    RelatedContentPage, RelationshipBatchRebuildRequest, RelationshipBatchRebuildResult,
    RelationshipBetweenResponse, RelationshipRebuildResult,
)
from app.services.relationship_service import MINIMUM_RELATIONSHIP_SCORE, RelationshipService

router = APIRouter(tags=["relationships"])


def _service(database: Session) -> RelationshipService:
    """Construct the service at the HTTP boundary without placing rules in routes."""

    return RelationshipService(database)


@router.post(
    "/relationships/contents/{content_id}/rebuild",
    response_model=RelationshipRebuildResult,
    summary="Rebuild deterministic relationships for one content",
    responses={404: {"description": "Content not found"}, 422: {"description": "Content is not eligible"}},
)
def rebuild_content_relationships(
    content_id: UUID,
    dry_run: bool = Query(default=False),
    database: Session = Depends(get_db),
) -> RelationshipRebuildResult:
    """Calculate and optionally persist the bounded canonical relationships for one item."""

    try:
        return _service(database).rebuild_content(content_id, dry_run=dry_run)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@router.post(
    "/relationships/rebuild",
    response_model=RelationshipBatchRebuildResult,
    summary="Rebuild deterministic relationships for a bounded batch",
    responses={422: {"description": "Invalid batch filters"}},
)
def rebuild_relationships_batch(
    request: RelationshipBatchRebuildRequest = Body(default_factory=RelationshipBatchRebuildRequest),
    database: Session = Depends(get_db),
) -> RelationshipBatchRebuildResult:
    """Run a safe, ordered batch rebuild with no unbounded API operation."""

    if request.published_after and request.published_before and request.published_after > request.published_before:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="published_after cannot exceed published_before")
    return _service(database).rebuild_batch(**request.model_dump())


@router.get(
    "/contents/{content_id}/related",
    response_model=RelatedContentPage,
    summary="List persisted content related to one content item",
    responses={404: {"description": "Content not found"}},
)
def related_contents(
    content_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    min_score: float | None = Query(default=None, ge=0, le=100),
    category: str | None = None,
    source_id: UUID | None = None,
    exclude_same_source: bool = False,
    algorithm_version: str | None = None,
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    database: Session = Depends(get_db),
) -> RelatedContentPage:
    """Expose either side of a canonical pair as the requested content's related item."""

    try:
        return _service(database).related(content_id, page=page, page_size=page_size, min_score=min_score, category=category, source_id=source_id, exclude_same_source=exclude_same_source, algorithm_version=algorithm_version, descending=sort_order == "desc")
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get(
    "/contents/{content_id}/recommendations",
    response_model=RelatedContentPage,
    summary="List deterministic, non-personalized content recommendations",
    responses={404: {"description": "Content not found"}},
)
def content_recommendations(
    content_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    min_score: float = Query(default=MINIMUM_RELATIONSHIP_SCORE, ge=0, le=100),
    exclude_same_source: bool = False,
    database: Session = Depends(get_db),
) -> RelatedContentPage:
    """Reuse related-content querying with recommendation-oriented defaults."""

    try:
        return _service(database).related(content_id, page=page, page_size=page_size, min_score=min_score, category=None, source_id=None, exclude_same_source=exclude_same_source, algorithm_version=None, descending=True)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get(
    "/relationships/between/{content_id}/{related_content_id}",
    response_model=RelationshipBetweenResponse,
    summary="Inspect one persisted relationship in either content order",
    responses={404: {"description": "Content or content relationship not found"}},
)
def relationship_between(
    content_id: UUID,
    related_content_id: UUID,
    database: Session = Depends(get_db),
) -> RelationshipBetweenResponse:
    """Return one canonical persisted pair, without triggering implicit recalculation."""

    try:
        return _service(database).between(content_id, related_content_id)
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

"""Routes for persisted ingested content."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.content import Content
from app.schemas.content import ContentRead
from app.services.enrichment_service import EnrichmentService

router = APIRouter(prefix="/contents", tags=["contents"])


@router.get("", response_model=list[ContentRead], summary="List and filter ingested content")
def list_contents(
    source_id: UUID | None = None,
    category: str | None = None,
    topic: str | None = None,
    language: str | None = None,
    processing_status: str | None = None,
    min_relevance_score: int | None = Query(default=None, ge=0, le=100),
    max_relevance_score: int | None = Query(default=None, ge=0, le=100),
    database: Session = Depends(get_db),
) -> list[Content]:
    """List persisted content using optional database-level filters."""

    if (
        min_relevance_score is not None
        and max_relevance_score is not None
        and min_relevance_score > max_relevance_score
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="min_relevance_score cannot exceed max_relevance_score")

    statement = select(Content)
    if source_id is not None:
        statement = statement.where(Content.source_id == source_id)
    if category is not None:
        statement = statement.where(Content.category == category)
    if topic is not None:
        statement = statement.where(cast(Content.topics, String).contains(f'"{topic}"'))
    if language is not None:
        statement = statement.where(Content.language == language)
    if processing_status is not None:
        statement = statement.where(Content.processing_status == processing_status)
    if min_relevance_score is not None:
        statement = statement.where(Content.relevance_score >= min_relevance_score)
    if max_relevance_score is not None:
        statement = statement.where(Content.relevance_score <= max_relevance_score)

    return list(database.scalars(statement.order_by(Content.created_at.desc())))


@router.get(
    "/{content_id}",
    response_model=ContentRead,
    summary="Get one ingested content item",
    responses={404: {"description": "Content not found"}},
)
def get_content(content_id: UUID, database: Session = Depends(get_db)) -> Content:
    """Return one persisted content item by UUID."""

    content = database.get(Content, content_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return content


@router.post(
    "/{content_id}/enrich",
    response_model=ContentRead,
    summary="Manually enrich one content item",
    responses={404: {"description": "Content not found"}},
)
def enrich_one_content(content_id: UUID, database: Session = Depends(get_db)) -> Content:
    """Explicitly enrich or reprocess a content item regardless of its status."""

    content = database.get(Content, content_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    EnrichmentService(database).enrich_content(content)
    database.refresh(content)
    return content

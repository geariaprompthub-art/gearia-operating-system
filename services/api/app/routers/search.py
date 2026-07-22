"""HTTP endpoint for indexed PostgreSQL full-text search."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.search import SearchResponse, SearchSortBy, SortOrder
from app.schemas.vector_search import VectorSearchRequest, VectorSearchResponse
from app.services.search_service import SearchService
from app.services.vector_search_dependencies import get_vector_search_service
from app.services.vector_search_service import VectorSearchService

router = APIRouter(prefix="/search", tags=["Search"])


@router.get(
    "",
    response_model=SearchResponse,
    summary="Search indexed contents",
    description="Search and filter indexed content using PostgreSQL Full-Text Search.",
    responses={422: {"description": "Invalid filter range or pagination parameter"}},
)
def search_contents(
    q: str | None = Query(default=None, max_length=300, description="Optional full-text query."),
    source_id: UUID | None = Query(default=None),
    category: str | None = Query(default=None),
    topic: str | None = Query(default=None, description="Exact topic membership."),
    language: str | None = Query(default=None),
    processing_status: str | None = Query(default=None),
    min_relevance_score: int | None = Query(default=None, ge=0, le=100),
    max_relevance_score: int | None = Query(default=None, ge=0, le=100),
    published_from: datetime | None = Query(default=None),
    published_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: SearchSortBy | None = Query(default=None),
    sort_order: SortOrder = Query(default=SortOrder.DESC),
    database: Session = Depends(get_db),
) -> SearchResponse:
    """Search indexed content with validated filters, ranking and SQL pagination."""

    if (
        min_relevance_score is not None
        and max_relevance_score is not None
        and min_relevance_score > max_relevance_score
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="min_relevance_score cannot exceed max_relevance_score")
    if published_from is not None and published_to is not None and published_from > published_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="published_from cannot exceed published_to")
    return SearchService(database).search(
        query=q, source_id=source_id, category=category, topic=topic, language=language,
        processing_status=processing_status, min_relevance_score=min_relevance_score,
        max_relevance_score=max_relevance_score, published_from=published_from,
        published_to=published_to, page=page, page_size=page_size,
        sort_by=sort_by, sort_order=sort_order,
    )


@router.post(
    "/vector",
    response_model=VectorSearchResponse,
    summary="Search content by exact cosine similarity",
    responses={
        422: {"description": "Invalid vector-search request"},
        503: {"description": "Embedding provider unavailable"},
    },
)
def search_vector(
    request: VectorSearchRequest,
    service: VectorSearchService = Depends(get_vector_search_service),
) -> dict[str, object]:
    """Perform side-effect-free vector retrieval using an ephemeral query vector."""

    try:
        return service.search(request.query, request.top_k, request.threshold)
    except RuntimeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vector search unavailable") from error

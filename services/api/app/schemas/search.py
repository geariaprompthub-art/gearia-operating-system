"""Pydantic schemas for PostgreSQL full-text content search."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SearchSortBy(str, Enum):
    """Allowed content search ordering fields."""

    RANK = "rank"
    RELEVANCE = "relevance"
    PUBLISHED_AT = "published_at"
    CREATED_AT = "created_at"


class SortOrder(str, Enum):
    """Allowed ordering directions."""

    ASC = "asc"
    DESC = "desc"


class SearchResultItem(BaseModel):
    """Public, lightweight representation of a matching content item."""

    id: UUID
    source_id: UUID
    title: str
    url: str
    summary: str | None
    author: str | None
    published_at: datetime | None
    language: str | None
    category: str | None
    topics: list[str]
    keywords: list[str]
    relevance_score: int | None
    processing_status: str
    created_at: datetime
    search_rank: float | None

    model_config = ConfigDict(from_attributes=True)


class SearchResponse(BaseModel):
    """Paginated response returned by text search."""

    query: str | None
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[SearchResultItem]

"""Pydantic contracts for deterministic content relationships."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.search import SearchResultItem


class RelationshipScoreBreakdown(BaseModel):
    """The deterministic-v1 contributions that compose a relationship score."""

    topics: float
    keywords: float
    category: float
    text: float
    temporal: float
    source: float


class ContentRelationshipRead(BaseModel):
    """Public, explainable representation of a persisted relationship."""

    id: UUID
    content_id: UUID
    related_content_id: UUID
    score: float = Field(ge=0, le=100)
    score_breakdown: RelationshipScoreBreakdown
    shared_topics: list[str]
    shared_keywords: list[str]
    same_category: bool
    same_source: bool
    text_similarity: float = Field(ge=0, le=1)
    published_distance_days: int | None
    reasons: list[str]
    algorithm_version: str
    calculated_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RelatedContentItem(BaseModel):
    """The other content side plus its relationship metadata."""

    content: SearchResultItem
    relationship: ContentRelationshipRead


class RelatedContentPage(BaseModel):
    """A page of relationships incident to a requested content item."""

    content_id: UUID
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[RelatedContentItem]


class RelationshipRebuildResult(BaseModel):
    """Outcome of rebuilding relationships for one content item."""

    content_id: UUID
    candidates_evaluated: int
    relationships_created: int
    relationships_updated: int
    relationships_deleted: int
    relationships_skipped: int
    relationships_unchanged: int
    duration_ms: int
    algorithm_version: str
    dry_run: bool
    errors: list[str]


class RelationshipBatchRebuildRequest(BaseModel):
    """Bounded batch rebuild parameters."""

    source_id: UUID | None = None
    category: str | None = None
    processing_status: str | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)
    dry_run: bool = False


class RelationshipBatchRebuildResult(BaseModel):
    """Aggregated outcome of a bounded batch rebuild."""

    contents_processed: int
    contents_succeeded: int
    contents_failed: int
    candidates_evaluated: int
    relationships_created: int
    relationships_updated: int
    relationships_deleted: int
    relationships_skipped: int
    relationships_unchanged: int
    duration_ms: int
    algorithm_version: str
    dry_run: bool
    errors: list[dict[str, str]]


class RelationshipBetweenResponse(BaseModel):
    """A persisted canonical pair and the two content records it connects."""

    relationship: ContentRelationshipRead
    content: SearchResultItem
    related_content: SearchResultItem

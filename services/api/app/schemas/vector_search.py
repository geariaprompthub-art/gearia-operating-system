"""Request and public response contracts for exact vector retrieval."""

from datetime import datetime
from math import isfinite
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VectorSearchRequest(BaseModel):
    """A normalized, bounded vector-search request."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=20, ge=1, le=100)
    threshold: float = Field(default=0.0, ge=-1.0, le=1.0)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """Strip user input and reject a query made only of whitespace."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized


class VectorSearchItem(BaseModel):
    """Public ranked result with no vector-bearing field."""

    content_id: UUID
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
    similarity: float
    rank: int

    model_config = ConfigDict(from_attributes=True)

    @field_validator("similarity")
    @classmethod
    def finite_similarity(cls, value: float) -> float:
        """Defend the public API against non-finite database values."""

        if not isfinite(value):
            raise ValueError("similarity must be finite")
        return value


class VectorSearchResponse(BaseModel):
    """Exact vector-search response; total always equals the returned item count."""

    query: str
    top_k: int
    threshold: float
    total: int
    items: list[VectorSearchItem]

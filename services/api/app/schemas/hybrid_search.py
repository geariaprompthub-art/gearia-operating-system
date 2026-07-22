"""Public contract for deterministic hybrid retrieval."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class HybridSearchRequest(BaseModel):
    """Strict hybrid-search request with no implicit parameters or type coercion."""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=8000)
    top_k: StrictInt = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized


class HybridSearchItem(BaseModel):
    """Safe hybrid result with no algorithm-specific scores or vectors."""

    rank: int
    content_id: UUID
    title: str
    url: str
    summary: str | None
    matched_by: list[str]


class HybridSearchResponse(BaseModel):
    """Public hybrid response; total always equals the returned item count."""

    items: list[HybridSearchItem]
    total: int

"""Pydantic schemas for ingested content."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ContentRead(BaseModel):
    """Serialized ingested content."""

    id: UUID
    source_id: UUID
    title: str
    url: str
    summary: str | None
    author: str | None
    published_at: datetime | None
    language: str | None
    fingerprint: str
    raw_payload: dict[str, Any]
    category: str | None
    topics: list[str]
    keywords: list[str]
    relevance_score: int | None
    processing_status: str
    processed_at: datetime | None
    processing_error: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ScoutRunResult(BaseModel):
    """Summary of one Scout ingestion execution."""

    sources_processed: int
    contents_created: int


class EnrichmentRunResult(BaseModel):
    """Summary of a batch enrichment execution."""

    contents_found: int
    contents_processed: int
    contents_failed: int

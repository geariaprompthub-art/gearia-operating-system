"""Persistence model for deterministic content relationships."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ContentRelationship(Base):
    """One canonical, explainable relationship between two content records."""

    __tablename__ = "content_relationships"
    __table_args__ = (
        UniqueConstraint("content_id", "related_content_id", name="uq_content_relationships_pair"),
        CheckConstraint("content_id <> related_content_id", name="ck_content_relationships_distinct_contents"),
        CheckConstraint("content_id < related_content_id", name="ck_content_relationships_canonical_pair"),
        CheckConstraint("score >= 0 AND score <= 100", name="ck_content_relationships_score_range"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    content_id: Mapped[UUID] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), index=True)
    related_content_id: Mapped[UUID] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), index=True)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    score_breakdown: Mapped[dict[str, float]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    shared_topics: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list)
    shared_keywords: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list)
    same_category: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    same_source: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    text_similarity: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    published_distance_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasons: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list)
    algorithm_version: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

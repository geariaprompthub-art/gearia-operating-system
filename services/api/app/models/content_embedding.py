"""Persisted immutable-identity embeddings for semantic retrieval."""
from datetime import UTC, datetime
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.db import Base

class ContentEmbedding(Base):
    __tablename__ = "content_embeddings"
    __table_args__ = (UniqueConstraint("content_id", "provider", "model", "text_strategy_version", name="uq_content_embeddings_identity"), CheckConstraint("dimensions = 1536", name="ck_content_embeddings_dimensions"), CheckConstraint("provider = 'openai'", name="ck_content_embeddings_provider"), CheckConstraint("model = 'text-embedding-3-small'", name="ck_content_embeddings_model"), CheckConstraint("text_strategy_version = 'content-text-v1'", name="ck_content_embeddings_strategy"))
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    content_id: Mapped[UUID] = mapped_column(ForeignKey("contents.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="text-embedding-3-small")
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=1536)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_strategy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="content-text-v1")
    embedding_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

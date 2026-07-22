"""PostgreSQL pgvector query boundary for exact cosine retrieval."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import literal, select
from sqlalchemy.orm import Session, load_only

from app.models.content import Content
from app.models.content_embedding import ContentEmbedding

EMBEDDING_PROVIDER = "openai"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
TEXT_STRATEGY_VERSION = "content-text-v1"


@dataclass(frozen=True)
class EmbeddingEligibilityRecord:
    """Metadata required to determine whether a persisted vector is usable."""

    content: Content
    content_hash: str | None
    status: str
    provider: str
    model: str
    dimensions: int
    text_strategy_version: str
    has_embedding: bool


@dataclass(frozen=True)
class VectorSearchCandidate:
    """Internal ranked vector candidate with the minimum hybrid-retrieval contract."""

    content_id: UUID
    similarity: float


@dataclass(frozen=True)
class VectorSearchRecord:
    """Content metadata used to hydrate a ranked vector candidate."""

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


class VectorSearchRepository:
    """Execute metadata prefiltering and exact PostgreSQL cosine retrieval."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def eligible_embedding_records(self) -> list[EmbeddingEligibilityRecord]:
        """Return metadata-only candidates; vector values are never loaded here."""

        statement = (
            select(ContentEmbedding, Content)
            .join(Content, Content.id == ContentEmbedding.content_id)
            .where(
                ContentEmbedding.embedding_status == "completed",
                ContentEmbedding.embedding.is_not(None),
                ContentEmbedding.provider == EMBEDDING_PROVIDER,
                ContentEmbedding.model == EMBEDDING_MODEL,
                ContentEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                ContentEmbedding.text_strategy_version == TEXT_STRATEGY_VERSION,
            )
            .options(
                load_only(
                    ContentEmbedding.id,
                    ContentEmbedding.content_id,
                    ContentEmbedding.content_hash,
                    ContentEmbedding.embedding_status,
                    ContentEmbedding.provider,
                    ContentEmbedding.model,
                    ContentEmbedding.dimensions,
                    ContentEmbedding.text_strategy_version,
                )
            )
        )
        return [
            EmbeddingEligibilityRecord(
                content=content,
                content_hash=embedding.content_hash,
                status=embedding.embedding_status,
                provider=embedding.provider,
                model=embedding.model,
                dimensions=embedding.dimensions,
                text_strategy_version=embedding.text_strategy_version,
                has_embedding=True,
            )
            for embedding, content in self._database.execute(statement)
        ]

    def search_candidates(
        self, query_vector: list[float], eligible_ids: list[UUID], top_k: int, threshold: float
    ) -> list[VectorSearchCandidate]:
        """Search only prevalidated IDs using PostgreSQL cosine distance."""

        if not eligible_ids:
            return []
        distance = ContentEmbedding.embedding.cosine_distance(query_vector)
        similarity = (literal(1.0) - distance).label("similarity")
        statement = (
            select(ContentEmbedding.content_id, similarity)
            .where(
                ContentEmbedding.content_id.in_(eligible_ids),
                ContentEmbedding.embedding_status == "completed",
                ContentEmbedding.embedding.is_not(None),
                ContentEmbedding.provider == EMBEDDING_PROVIDER,
                ContentEmbedding.model == EMBEDDING_MODEL,
                ContentEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                ContentEmbedding.text_strategy_version == TEXT_STRATEGY_VERSION,
                similarity >= threshold,
            )
            .order_by(distance.asc(), ContentEmbedding.content_id.asc())
            .limit(top_k)
        )
        return [
            VectorSearchCandidate(content_id=content_id, similarity=float(score))
            for content_id, score in self._database.execute(statement)
        ]

    def hydrate(self, content_ids: list[UUID]) -> dict[UUID, VectorSearchRecord]:
        """Fetch content metadata once; callers preserve the ranked candidate order."""

        if not content_ids:
            return {}
        rows = self._database.scalars(select(Content).where(Content.id.in_(content_ids)))
        return {
            content.id: VectorSearchRecord(
                content_id=content.id,
                source_id=content.source_id,
                title=content.title,
                url=content.url,
                summary=content.summary,
                author=content.author,
                published_at=content.published_at,
                language=content.language,
                category=content.category,
                topics=content.topics or [],
                keywords=content.keywords or [],
                relevance_score=content.relevance_score,
                processing_status=content.processing_status,
                created_at=content.created_at,
            )
            for content in rows
        }

"""Application service for exact, side-effect-free vector retrieval."""

from math import isfinite

from app.repositories.vector_search_repository import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    TEXT_STRATEGY_VERSION,
    VectorSearchCandidate,
    VectorSearchRecord,
    VectorSearchRepository,
)
from app.services.embedding_provider import EmbeddingProvider, build_content_embedding_text, content_hash


class VectorSearchService:
    """Generate an ephemeral query vector and retrieve only usable content."""

    def __init__(self, repository: VectorSearchRepository, provider: EmbeddingProvider) -> None:
        self._repository = repository
        self._provider = provider

    @staticmethod
    def _is_eligible(candidate: VectorSearchCandidate) -> bool:
        """Apply fixed compatibility and current-hash checks before vector SQL."""

        current_hash = content_hash(build_content_embedding_text(candidate.content))
        return (
            candidate.status == "completed"
            and candidate.has_embedding
            and candidate.provider == EMBEDDING_PROVIDER
            and candidate.model == EMBEDDING_MODEL
            and candidate.dimensions == EMBEDDING_DIMENSIONS
            and candidate.text_strategy_version == TEXT_STRATEGY_VERSION
            and candidate.content_hash == current_hash
        )

    def search(self, query: str, top_k: int, threshold: float) -> dict[str, object]:
        """Return ranked public metadata without mutating content or embeddings."""

        normalized_query = query.strip()
        vector = self._provider.embed_text(normalized_query)
        if len(vector) != EMBEDDING_DIMENSIONS or not all(isfinite(float(value)) for value in vector):
            raise RuntimeError("Invalid query embedding")
        eligible_ids = [
            candidate.content.id
            for candidate in self._repository.eligible_candidates()
            if self._is_eligible(candidate)
        ]
        if not eligible_ids:
            return {"query": normalized_query, "top_k": top_k, "threshold": threshold, "total": 0, "items": []}
        records = self._repository.search(vector, eligible_ids, top_k, threshold)
        finite_records = [record for record in records if isfinite(record.similarity)]
        items = [self._public_item(record, rank) for rank, record in enumerate(finite_records, start=1)]
        return {"query": normalized_query, "top_k": top_k, "threshold": threshold, "total": len(items), "items": items}

    @staticmethod
    def _public_item(record: VectorSearchRecord, rank: int) -> dict[str, object]:
        """Map a repository record to safe response metadata."""

        return {
            "content_id": record.content_id,
            "source_id": record.source_id,
            "title": record.title,
            "url": record.url,
            "summary": record.summary,
            "author": record.author,
            "published_at": record.published_at,
            "language": record.language,
            "category": record.category,
            "topics": record.topics,
            "keywords": record.keywords,
            "relevance_score": record.relevance_score,
            "processing_status": record.processing_status,
            "created_at": record.created_at,
            "similarity": record.similarity,
            "rank": rank,
        }

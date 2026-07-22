"""Application service for exact, side-effect-free vector retrieval."""

from math import isfinite

from app.repositories.vector_search_repository import VectorSearchCandidate, VectorSearchRecord, VectorSearchRepository
from app.services.embedding_provider import EmbeddingProvider
from app.services.vector_candidate_service import VectorCandidateService


class VectorSearchService:
    """Generate an ephemeral query vector and retrieve only usable content."""

    def __init__(self, repository: VectorSearchRepository, provider: EmbeddingProvider) -> None:
        self._repository = repository
        self._candidates = VectorCandidateService(repository, provider)

    def search(self, query: str, top_k: int, threshold: float) -> dict[str, object]:
        """Return ranked public metadata without mutating content or embeddings."""

        normalized_query = query.strip()
        candidates = self._candidates.search(normalized_query, top_k, threshold)
        records = self._repository.hydrate([candidate.content_id for candidate in candidates])
        finite_candidates = [candidate for candidate in candidates if isfinite(candidate.similarity) and candidate.content_id in records]
        items = [self._public_item(records[candidate.content_id], candidate, rank) for rank, candidate in enumerate(finite_candidates, start=1)]
        return {"query": normalized_query, "top_k": top_k, "threshold": threshold, "total": len(items), "items": items}

    @staticmethod
    def _public_item(record: VectorSearchRecord, candidate: VectorSearchCandidate, rank: int) -> dict[str, object]:
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
            "similarity": candidate.similarity,
            "rank": rank,
        }

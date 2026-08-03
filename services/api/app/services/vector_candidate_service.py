"""Shared internal vector-candidate retrieval for exact and hybrid search."""

from collections.abc import Sequence
from math import isfinite
from uuid import UUID

from app.repositories.vector_search_repository import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    TEXT_STRATEGY_VERSION,
    EmbeddingEligibilityRecord,
    VectorSearchCandidate,
    VectorSearchRepository,
)
from app.services.embedding_provider import EmbeddingProvider, build_content_embedding_text, content_hash


class VectorCandidateService:
    """Generate one ephemeral query vector and return eligible ranked candidates."""

    def __init__(self, repository: VectorSearchRepository, provider: EmbeddingProvider) -> None:
        self._repository = repository
        self._provider = provider

    @staticmethod
    def _is_eligible(record: EmbeddingEligibilityRecord) -> bool:
        """Apply the frozen Sprint 08 compatibility and stale rules."""

        current_hash = content_hash(build_content_embedding_text(record.content))
        return (
            record.status == "completed"
            and record.has_embedding
            and record.provider == EMBEDDING_PROVIDER
            and record.model == EMBEDDING_MODEL
            and record.dimensions == EMBEDDING_DIMENSIONS
            and record.text_strategy_version == TEXT_STRATEGY_VERSION
            and record.content_hash == current_hash
        )

    def search(
        self,
        query: str,
        candidate_k: int,
        threshold: float,
        visible_content_ids: Sequence[UUID] | None = None,
    ) -> list[VectorSearchCandidate]:
        """Return ordered candidates without persistence or public-schema concerns."""

        vector = self._provider.embed_text(query.strip())
        if len(vector) != EMBEDDING_DIMENSIONS or not all(isfinite(float(value)) for value in vector):
            raise RuntimeError("Invalid query embedding")
        records = (
            self._repository.eligible_embedding_records()
            if visible_content_ids is None
            else self._repository.eligible_embedding_records(visible_content_ids)
        )
        eligible_ids = [record.content.id for record in records if self._is_eligible(record)]
        if not eligible_ids:
            return []
        return self._repository.search_candidates(vector, eligible_ids, candidate_k, threshold)

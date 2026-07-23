"""Injected internal reranking stage, intentionally independent from Hybrid retrieval."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_hydration_repository import ContentHydrationRepository
from app.repositories.rerank_document_repository import RerankDocumentRepository
from app.services.rerank_document_formatter import RerankDocument, RerankDocumentFormatter
from app.services.reranking_contracts import RerankCandidate
from app.services.reranking_service import RerankingService


@dataclass(frozen=True, slots=True)
class MergedRerankCandidate:
    content_id: UUID
    matched_by: tuple[str, ...]


class HybridRerankingPipeline:
    """Apply the frozen reranking sub-pipeline to already merged candidates."""

    candidate_pool_limit = 100

    def __init__(self, eligibility_repository: ContentEligibilityRepository, rerank_document_repository: RerankDocumentRepository, formatter: RerankDocumentFormatter, reranking_service: RerankingService, hydration_repository: ContentHydrationRepository) -> None:
        self._eligibility = eligibility_repository; self._documents = rerank_document_repository
        self._formatter = formatter; self._reranker = reranking_service; self._hydration = hydration_repository

    def run(self, query: str, merged_candidates: Sequence[MergedRerankCandidate], top_k: int) -> dict[str, object]:
        candidates = list(merged_candidates)
        eligible_ids = self._eligibility.filter_eligible([candidate.content_id for candidate in candidates])
        if not eligible_ids:
            return {"items": [], "total": 0}
        by_id = {candidate.content_id: candidate for candidate in candidates}
        pool_ids = eligible_ids[:self.candidate_pool_limit]
        hydrated_documents = self._documents.hydrate(pool_ids)
        documents_by_id = {document.content_id: document for document in hydrated_documents}
        documents = [documents_by_id[content_id] for content_id in pool_ids if content_id in documents_by_id]
        rerank_candidates = [
            RerankCandidate(record.content_id, self._formatter.format(RerankDocument(record.title, record.summary, record.category, record.topics, record.keywords)), rank, by_id[record.content_id].matched_by)
            for rank, record in enumerate(documents, start=1)
        ]
        reranked = self._reranker.rerank(query, rerank_candidates) if rerank_candidates else []
        final = reranked[:top_k]
        hydrated = self._hydration.hydrate([candidate.content_id for candidate in final]) if final else []
        provenance = {candidate.content_id: candidate.matched_by for candidate in final}
        items = [{"rank": rank, "content_id": content.content_id, "title": content.title, "url": content.url, "summary": content.summary, "matched_by": list(provenance[content.content_id])} for rank, content in enumerate(hydrated, 1)]
        return {"items": items, "total": len(items)}

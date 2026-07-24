"""Injected internal reranking stage, intentionally independent from Hybrid retrieval."""

from collections.abc import Sequence
from uuid import UUID

from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_hydration_repository import ContentHydrationRepository, HydratedContent
from app.repositories.rerank_document_repository import RerankDocumentRecord, RerankDocumentRepository
from app.services.rerank_document_formatter import RerankDocument, RerankDocumentFormatter
from app.services.reranking_contracts import RerankCandidate
from app.services.reranking_service import RerankingService
from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.pre_reranking_candidate_pool import PreRerankingCandidatePool
from app.services.reciprocal_rank_fusion import FusedCandidate


class RerankingPipelineHydrationError(RuntimeError):
    """Raised when a read-only hydration response cannot represent the requested IDs."""


class HybridRerankingPipeline:
    """Apply the frozen reranking sub-pipeline to RRF and Graph candidates."""

    def __init__(
        self,
        eligibility_repository: ContentEligibilityRepository,
        rerank_document_repository: RerankDocumentRepository,
        formatter: RerankDocumentFormatter,
        reranking_service: RerankingService,
        hydration_repository: ContentHydrationRepository,
        candidate_pool: type[PreRerankingCandidatePool],
    ) -> None:
        self._eligibility = eligibility_repository
        self._documents = rerank_document_repository
        self._formatter = formatter
        self._reranker = reranking_service
        self._hydration = hydration_repository
        self._candidate_pool = candidate_pool

    def run(
        self,
        query: str,
        rrf_candidates: Sequence[FusedCandidate],
        graph_candidates: Sequence[GraphExpandedCandidate],
        top_k: int,
    ) -> dict[str, object]:
        """Rerank the ADR-approved consolidated pool without any fallback."""

        candidates = self._candidate_pool.build(rrf_candidates, graph_candidates, top_k)
        eligible_ids = self._eligibility.filter_eligible([candidate.content_id for candidate in candidates])
        if not eligible_ids:
            return {"items": [], "total": 0}
        by_id = {candidate.content_id: candidate for candidate in candidates}
        pool_ids = eligible_ids
        hydrated_documents = self._documents.hydrate(pool_ids)
        documents_by_id = self._validate_document_hydration(hydrated_documents, pool_ids)
        documents = [documents_by_id[content_id] for content_id in pool_ids]
        rerank_candidates = [
            RerankCandidate(record.content_id, self._formatter.format(RerankDocument(record.title, record.summary, record.category, record.topics, record.keywords)), rank, by_id[record.content_id].matched_by)
            for rank, record in enumerate(documents, start=1)
        ]
        reranked = self._reranker.rerank(query, rerank_candidates) if rerank_candidates else []
        final = reranked[:top_k]
        final_ids = [candidate.content_id for candidate in final]
        hydrated = self._hydration.hydrate(final_ids) if final else []
        contents_by_id = self._validate_public_hydration(hydrated, final_ids)
        ordered_contents = [contents_by_id[content_id] for content_id in final_ids]
        provenance = {candidate.content_id: candidate.matched_by for candidate in final}
        items = [
            {
                "rank": rank,
                "content_id": content.content_id,
                "title": content.title,
                "url": content.url,
                "summary": content.summary,
                "matched_by": list(provenance[content.content_id]),
            }
            for rank, content in enumerate(ordered_contents, 1)
        ]
        return {"items": items, "total": len(items)}

    @staticmethod
    def _validate_document_hydration(
        documents: Sequence[RerankDocumentRecord], requested_ids: Sequence[UUID]
    ) -> dict[UUID, RerankDocumentRecord]:
        expected_ids = set(requested_ids)
        if len(documents) != len(requested_ids):
            raise RerankingPipelineHydrationError(
                "Reranking document hydration returned an unexpected result count"
            )

        documents_by_id: dict[UUID, RerankDocumentRecord] = {}
        for document in documents:
            if not isinstance(document, RerankDocumentRecord) or not isinstance(document.content_id, UUID):
                raise RerankingPipelineHydrationError(
                    "Reranking document hydration returned an invalid response"
                )
            if document.content_id not in expected_ids:
                raise RerankingPipelineHydrationError(
                    "Reranking document hydration returned an unknown content identifier"
                )
            if document.content_id in documents_by_id:
                raise RerankingPipelineHydrationError(
                    "Reranking document hydration returned duplicate content identifiers"
                )
            documents_by_id[document.content_id] = document

        if set(documents_by_id) != expected_ids:
            raise RerankingPipelineHydrationError(
                "Reranking document hydration returned an unexpected result set"
            )
        return documents_by_id

    @staticmethod
    def _validate_public_hydration(
        contents: Sequence[HydratedContent], requested_ids: Sequence[UUID]
    ) -> dict[UUID, HydratedContent]:
        expected_ids = set(requested_ids)
        if len(contents) != len(requested_ids):
            raise RerankingPipelineHydrationError(
                "Public content hydration returned an unexpected result count"
            )

        contents_by_id: dict[UUID, HydratedContent] = {}
        for content in contents:
            if not isinstance(content, HydratedContent) or not isinstance(content.content_id, UUID):
                raise RerankingPipelineHydrationError(
                    "Public content hydration returned an invalid response"
                )
            if content.content_id not in expected_ids:
                raise RerankingPipelineHydrationError(
                    "Public content hydration returned an unknown content identifier"
                )
            if content.content_id in contents_by_id:
                raise RerankingPipelineHydrationError(
                    "Public content hydration returned duplicate content identifiers"
                )
            contents_by_id[content.content_id] = content

        if set(contents_by_id) != expected_ids:
            raise RerankingPipelineHydrationError(
                "Public content hydration returned an unexpected result set"
            )
        return contents_by_id

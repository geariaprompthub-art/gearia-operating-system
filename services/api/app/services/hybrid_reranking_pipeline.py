"""Injected internal reranking stage, intentionally independent from Hybrid retrieval."""

from collections.abc import Sequence
from time import perf_counter
from uuid import UUID

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_hydration_repository import ContentHydrationRepository, HydratedContent
from app.repositories.rerank_document_repository import RerankDocumentRecord, RerankDocumentRepository
from app.services.rerank_document_formatter import RerankDocument, RerankDocumentFormatter
from app.services.reranking_contracts import RerankCandidate
from app.services.reranking_service import RerankingService
from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.hybrid_search_telemetry import HybridSearchStage, HybridSearchTelemetry, NoOpHybridSearchTelemetry, emit_safely
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
        telemetry: HybridSearchTelemetry = NoOpHybridSearchTelemetry(),
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._eligibility = eligibility_repository
        self._documents = rerank_document_repository
        self._formatter = formatter
        self._reranker = reranking_service
        self._hydration = hydration_repository
        self._candidate_pool = candidate_pool
        self._telemetry = telemetry
        self._structured_logger = structured_logger

    def run(
        self,
        query: str,
        rrf_candidates: Sequence[FusedCandidate],
        graph_candidates: Sequence[GraphExpandedCandidate],
        top_k: int,
        candidate_limit: int,
    ) -> dict[str, object]:
        """O provider recebe todos os candidatos elegíveis selecionados pelo PreRerankingCandidatePool, limitados pelo horizonte H."""

        self._validate_candidate_limit(candidate_limit)
        pool_started = perf_counter()
        self._stage_started(HybridSearchStage.CANDIDATE_POOL)
        try:
            candidates = self._candidate_pool.build(rrf_candidates, graph_candidates, candidate_limit)
        except Exception as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.CANDIDATE_POOL, perf_counter() - pool_started, "unexpected")
            self._stage_failed(HybridSearchStage.CANDIDATE_POOL, pool_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.CANDIDATE_POOL, perf_counter() - pool_started, input_count=len(rrf_candidates) + len(graph_candidates), output_count=len(candidates))
        self._stage_completed(HybridSearchStage.CANDIDATE_POOL, pool_started, candidate_count=len(candidates))
        eligibility_started = perf_counter()
        self._stage_started(HybridSearchStage.ELIGIBILITY)
        try:
            eligible_ids = self._eligibility.filter_eligible([candidate.content_id for candidate in candidates])
        except Exception as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.ELIGIBILITY, perf_counter() - eligibility_started, "unexpected")
            self._stage_failed(HybridSearchStage.ELIGIBILITY, eligibility_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.ELIGIBILITY, perf_counter() - eligibility_started, input_count=len(candidates), output_count=len(eligible_ids))
        self._stage_completed(HybridSearchStage.ELIGIBILITY, eligibility_started, eligible_count=len(eligible_ids))
        if not eligible_ids:
            return {"items": [], "total": 0}
        by_id = {candidate.content_id: candidate for candidate in candidates}
        pool_ids = eligible_ids
        document_hydration_started = perf_counter()
        self._stage_started(HybridSearchStage.RERANKING_HYDRATION)
        try:
            hydrated_documents = self._documents.hydrate(pool_ids)
            documents_by_id = self._validate_document_hydration(hydrated_documents, pool_ids)
        except Exception as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.RERANKING_HYDRATION, perf_counter() - document_hydration_started, "hydration")
            self._stage_failed(HybridSearchStage.RERANKING_HYDRATION, document_hydration_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.RERANKING_HYDRATION, perf_counter() - document_hydration_started, input_count=len(pool_ids), output_count=len(hydrated_documents))
        self._stage_completed(HybridSearchStage.RERANKING_HYDRATION, document_hydration_started, hydrated_count=len(hydrated_documents))
        documents = [documents_by_id[content_id] for content_id in pool_ids]
        formatting_started = perf_counter()
        self._stage_started(HybridSearchStage.DOCUMENT_FORMATTING)
        try:
            rerank_candidates = [
                RerankCandidate(record.content_id, self._formatter.format(RerankDocument(record.title, record.summary, record.category, record.topics, record.keywords)), rank, by_id[record.content_id].matched_by)
                for rank, record in enumerate(documents, start=1)
            ]
        except Exception as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.DOCUMENT_FORMATTING, perf_counter() - formatting_started, "unexpected")
            self._stage_failed(HybridSearchStage.DOCUMENT_FORMATTING, formatting_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.DOCUMENT_FORMATTING, perf_counter() - formatting_started, input_count=len(documents), output_count=len(rerank_candidates))
        self._stage_completed(HybridSearchStage.DOCUMENT_FORMATTING, formatting_started, formatted_count=len(rerank_candidates))
        provider_started = perf_counter()
        self._stage_started(HybridSearchStage.PROVIDER_RERANKING)
        try:
            reranked = self._reranker.rerank(query, rerank_candidates) if rerank_candidates else []
        except Exception as error:
            if rerank_candidates:
                emit_safely(self._telemetry.record_provider_call, perf_counter() - provider_started, input_count=len(rerank_candidates), output_count=0, status="error")
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.PROVIDER_RERANKING, perf_counter() - provider_started, "provider")
            self._stage_failed(HybridSearchStage.PROVIDER_RERANKING, provider_started, error)
            raise
        if rerank_candidates:
            emit_safely(self._telemetry.record_provider_call, perf_counter() - provider_started, input_count=len(rerank_candidates), output_count=len(reranked), status="success")
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.PROVIDER_RERANKING, perf_counter() - provider_started, input_count=len(rerank_candidates), output_count=len(reranked))
        self._stage_completed(HybridSearchStage.PROVIDER_RERANKING, provider_started, reranked_count=len(reranked))
        final_started = perf_counter()
        self._stage_started(HybridSearchStage.FINAL_TOP_K)
        try:
            final = reranked[:top_k]
        except Exception as error:
            self._stage_failed(HybridSearchStage.FINAL_TOP_K, final_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.FINAL_TOP_K, perf_counter() - final_started, input_count=len(reranked), output_count=len(final))
        self._stage_completed(HybridSearchStage.FINAL_TOP_K, final_started, reranked_count=len(final))
        final_ids = [candidate.content_id for candidate in final]
        public_hydration_started = perf_counter()
        self._stage_started(HybridSearchStage.PUBLIC_HYDRATION)
        try:
            hydrated = self._hydration.hydrate(final_ids) if final else []
            contents_by_id = self._validate_public_hydration(hydrated, final_ids)
        except Exception as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.PUBLIC_HYDRATION, perf_counter() - public_hydration_started, "hydration")
            self._stage_failed(HybridSearchStage.PUBLIC_HYDRATION, public_hydration_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.PUBLIC_HYDRATION, perf_counter() - public_hydration_started, input_count=len(final_ids), output_count=len(hydrated))
        self._stage_completed(HybridSearchStage.PUBLIC_HYDRATION, public_hydration_started, hydrated_count=len(hydrated))
        ordered_contents = [contents_by_id[content_id] for content_id in final_ids]
        provenance = {candidate.content_id: candidate.matched_by for candidate in final}
        response_started = perf_counter()
        self._stage_started(HybridSearchStage.RESPONSE_BUILDING)
        try:
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
            result = {"items": items, "total": len(items)}
        except Exception as error:
            self._stage_failed(HybridSearchStage.RESPONSE_BUILDING, response_started, error)
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.RESPONSE_BUILDING, perf_counter() - response_started, input_count=len(ordered_contents), output_count=len(items))
        self._stage_completed(HybridSearchStage.RESPONSE_BUILDING, response_started)
        return result

    def _stage_started(self, stage: str) -> None:
        """Emit the pipeline-owned start event without user or content data."""

        if self._structured_logger is not None:
            self._structured_logger.info(LogEvent.HYBRID_PIPELINE_STAGE_STARTED, "Hybrid pipeline stage started", stage=stage)

    def _stage_completed(self, stage: str, started: float, **counts: int) -> None:
        """Emit the pipeline-owned completion event with bounded aggregate counts."""

        if self._structured_logger is not None:
            self._structured_logger.info(LogEvent.HYBRID_PIPELINE_STAGE_COMPLETED, "Hybrid pipeline stage completed", stage=stage, duration_ms=(perf_counter() - started) * 1000, **counts)

    def _stage_failed(self, stage: str, started: float, error: Exception) -> None:
        """Emit one safe failure event and preserve the original exception."""

        if self._structured_logger is not None:
            self._structured_logger.error(LogEvent.HYBRID_PIPELINE_STAGE_FAILED, "Hybrid pipeline stage failed", stage=stage, duration_ms=(perf_counter() - started) * 1000, error_type=type(error).__name__)

    @staticmethod
    def _validate_candidate_limit(candidate_limit: int) -> None:
        """Reject ambiguous or provider-unsafe pre-reranking limits."""

        if type(candidate_limit) is not int or not 0 < candidate_limit <= RerankingService.MAX_CANDIDATES:
            raise ValueError(
                f"candidate_limit must be an integer between 1 and {RerankingService.MAX_CANDIDATES}"
            )

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

"""Sequential, fail-closed composition of lexical, vector, RRF and Graph retrieval."""

from dataclasses import dataclass
from time import perf_counter
from sqlalchemy.exc import SQLAlchemyError

from app.repositories.lexical_search_repository import LexicalSearchRepository
from app.services.graph_expansion_service import GraphExpansionService
from app.services.hybrid_reranking_pipeline import HybridRerankingPipeline
from app.services.hybrid_search_telemetry import HybridSearchStage, HybridSearchTelemetry, NoOpHybridSearchTelemetry, emit_safely
from app.services.reciprocal_rank_fusion import RankedList, ReciprocalRankFusion
from app.services.reranking_service import RerankingService
from app.services.vector_candidate_service import VectorCandidateService


@dataclass(frozen=True)
class HybridSearchSettings:
    """Internal candidate and fusion limits; never part of the public request."""

    lexical_candidate_k: int = 50
    vector_candidate_k: int = 50
    rrf_k: int = 60


class HybridSearchService:
    """Orchestrate lexical, vector and Graph backfill without persistence or fallback."""

    def __init__(
        self,
        lexical_repository: LexicalSearchRepository,
        vector_candidates: VectorCandidateService,
        graph_expansion_service: GraphExpansionService,
        reranking_pipeline: HybridRerankingPipeline,
        settings: HybridSearchSettings = HybridSearchSettings(),
        telemetry: HybridSearchTelemetry = NoOpHybridSearchTelemetry(),
    ) -> None:
        self._lexical_repository = lexical_repository
        self._vector_candidates = vector_candidates
        self._graph_expansion_service = graph_expansion_service
        self._reranking_pipeline = reranking_pipeline
        self._settings = settings
        self._telemetry = telemetry

    def search(self, query: str, top_k: int) -> dict[str, object]:
        """Run retrieval, Graph backfill and one final ordered hydration."""

        started = perf_counter()
        emit_safely(self._telemetry.record_request_started)
        normalized_query = query.strip()
        lexical_limit = self._settings.lexical_candidate_k
        vector_limit = self._settings.vector_candidate_k
        rrf_candidate_limit = min(
            RerankingService.MAX_CANDIDATES,
            lexical_limit + vector_limit,
        )
        lexical_started = perf_counter()
        try:
            lexical = self._lexical_repository.search(normalized_query, lexical_limit)
        except SQLAlchemyError as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.LEXICAL_RETRIEVAL, perf_counter() - lexical_started, "retrieval")
            self._record_request_failure(started, "retrieval")
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.LEXICAL_RETRIEVAL, perf_counter() - lexical_started, input_count=0, output_count=len(lexical))
        vector_started = perf_counter()
        try:
            vector = self._vector_candidates.search(normalized_query, vector_limit, -1.0)
        except SQLAlchemyError as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.VECTOR_RETRIEVAL, perf_counter() - vector_started, "retrieval")
            self._record_request_failure(started, "retrieval")
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.VECTOR_RETRIEVAL, perf_counter() - vector_started, input_count=0, output_count=len(vector))
        rrf_started = perf_counter()
        try:
            fused = ReciprocalRankFusion.fuse(
                [RankedList(origin="lexical", candidates=lexical), RankedList(origin="vector", candidates=vector)],
                top_k=rrf_candidate_limit,
                rrf_k=self._settings.rrf_k,
            )
        except Exception:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.RECIPROCAL_RANK_FUSION, perf_counter() - rrf_started, "unexpected")
            self._record_request_failure(started, "unexpected")
            raise
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.RECIPROCAL_RANK_FUSION, perf_counter() - rrf_started, input_count=len(lexical) + len(vector), output_count=len(fused))
        graph_started = perf_counter()
        try:
            graph_candidates = self._graph_expansion_service.expand(
                [candidate.content_id for candidate in fused]
            )
        except SQLAlchemyError as error:
            emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.GRAPH_EXPANSION, perf_counter() - graph_started, "graph")
            self._record_request_failure(started, "graph")
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.GRAPH_EXPANSION, perf_counter() - graph_started, input_count=len(fused), output_count=len(graph_candidates))
        try:
            result = self._reranking_pipeline.run(normalized_query, fused, graph_candidates, top_k, candidate_limit=rrf_candidate_limit)
        except Exception:
            self._record_request_failure(started, "unexpected")
            raise
        duration = perf_counter() - started
        emit_safely(self._telemetry.record_stage_completed, HybridSearchStage.HYBRID_SEARCH_TOTAL, duration, input_count=0, output_count=int(result["total"]))
        emit_safely(self._telemetry.record_request_completed, duration, final_item_count=int(result["total"]), status="success")
        return result

    def _record_request_failure(self, started: float, error_type: str) -> None:
        """Emit terminal failure telemetry without obscuring the functional error."""

        duration = perf_counter() - started
        emit_safely(self._telemetry.record_stage_failed, HybridSearchStage.HYBRID_SEARCH_TOTAL, duration, error_type)
        emit_safely(self._telemetry.record_request_completed, duration, final_item_count=0, status="error")

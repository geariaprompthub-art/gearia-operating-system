"""Sequential, fail-closed composition of lexical, vector, RRF and Graph retrieval."""

from dataclasses import dataclass
from sqlalchemy.exc import SQLAlchemyError

from app.repositories.lexical_search_repository import LexicalSearchRepository
from app.services.graph_expansion_service import GraphExpansionService
from app.services.hybrid_reranking_pipeline import HybridRerankingPipeline
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
    ) -> None:
        self._lexical_repository = lexical_repository
        self._vector_candidates = vector_candidates
        self._graph_expansion_service = graph_expansion_service
        self._reranking_pipeline = reranking_pipeline
        self._settings = settings

    def search(self, query: str, top_k: int) -> dict[str, object]:
        """Run retrieval, Graph backfill and one final ordered hydration."""

        normalized_query = query.strip()
        lexical_limit = self._settings.lexical_candidate_k
        vector_limit = self._settings.vector_candidate_k
        rrf_candidate_limit = min(
            RerankingService.MAX_CANDIDATES,
            lexical_limit + vector_limit,
        )
        try:
            lexical = self._lexical_repository.search(normalized_query, lexical_limit)
            vector = self._vector_candidates.search(normalized_query, vector_limit, -1.0)
        except SQLAlchemyError as error:
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        fused = ReciprocalRankFusion.fuse(
            [RankedList(origin="lexical", candidates=lexical), RankedList(origin="vector", candidates=vector)],
            top_k=rrf_candidate_limit,
            rrf_k=self._settings.rrf_k,
        )
        try:
            graph_candidates = self._graph_expansion_service.expand(
                [candidate.content_id for candidate in fused]
            )
        except SQLAlchemyError as error:
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        return self._reranking_pipeline.run(
            normalized_query,
            fused,
            graph_candidates,
            top_k,
            candidate_limit=rrf_candidate_limit,
        )

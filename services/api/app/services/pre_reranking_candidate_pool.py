"""Pure formation of the bounded RRF and Graph candidate pool."""

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from uuid import UUID

from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.reciprocal_rank_fusion import FusedCandidate


@dataclass(frozen=True, slots=True)
class ConsolidatedPoolCandidate:
    """Candidate metadata retained for the future reranking pipeline boundary."""

    content_id: UUID
    matched_by: tuple[str, ...]


class PreRerankingCandidatePool:
    """Build the approved bounded pool without ranking, hydration, or providers."""

    CAP = 100
    MINIMUM_HORIZON = 20
    OVERFETCH_FACTOR = 5
    GRAPH_SHARE = 0.20

    @classmethod
    def build(
        cls,
        rrf_candidates: Sequence[FusedCandidate],
        graph_candidates: Sequence[GraphExpandedCandidate],
        top_k: int,
    ) -> list[ConsolidatedPoolCandidate]:
        """Form the RRF-first, Graph-reserved candidate pool prescribed by ADR-001."""

        cls._validate_top_k(top_k)
        horizon = cls.horizon(top_k)
        graph_budget = ceil(horizon * cls.GRAPH_SHARE)
        rrf_budget = horizon - graph_budget

        rrf = cls._unique_rrf(rrf_candidates)
        rrf_ids = {candidate.content_id for candidate in rrf}
        graph = cls._unique_graph(graph_candidates, rrf_ids)

        selected_rrf = rrf[:rrf_budget]
        selected_graph = graph[:graph_budget]
        result = [*selected_rrf, *selected_graph]

        if len(selected_graph) < graph_budget:
            result.extend(rrf[rrf_budget : rrf_budget + (graph_budget - len(selected_graph))])
        if len(selected_rrf) < rrf_budget:
            result.extend(graph[graph_budget : graph_budget + (rrf_budget - len(selected_rrf))])

        return result[:horizon]

    @classmethod
    def horizon(cls, top_k: int) -> int:
        """Return ``H = min(100, max(20, 5 * top_k))`` after strict validation."""

        cls._validate_top_k(top_k)
        return min(cls.CAP, max(cls.MINIMUM_HORIZON, cls.OVERFETCH_FACTOR * top_k))

    @staticmethod
    def _validate_top_k(top_k: int) -> None:
        if type(top_k) is not int or top_k < 1:
            raise ValueError("top_k must be a positive integer")

    @staticmethod
    def _unique_rrf(candidates: Sequence[FusedCandidate]) -> list[ConsolidatedPoolCandidate]:
        values: list[ConsolidatedPoolCandidate] = []
        seen_ids: set[UUID] = set()
        for candidate in candidates:
            if not isinstance(candidate, FusedCandidate) or not isinstance(candidate.content_id, UUID):
                raise ValueError("rrf_candidates must contain valid FusedCandidate values")
            if candidate.content_id not in seen_ids:
                seen_ids.add(candidate.content_id)
                values.append(ConsolidatedPoolCandidate(candidate.content_id, candidate.matched_by))
        return values

    @staticmethod
    def _unique_graph(
        candidates: Sequence[GraphExpandedCandidate], rrf_ids: set[UUID]
    ) -> list[ConsolidatedPoolCandidate]:
        values: list[ConsolidatedPoolCandidate] = []
        seen_ids: set[UUID] = set(rrf_ids)
        for candidate in candidates:
            if not isinstance(candidate, GraphExpandedCandidate) or not isinstance(candidate.content_id, UUID):
                raise ValueError("graph_candidates must contain valid GraphExpandedCandidate values")
            if candidate.content_id not in seen_ids:
                seen_ids.add(candidate.content_id)
                values.append(ConsolidatedPoolCandidate(candidate.content_id, ("graph",)))
        return values

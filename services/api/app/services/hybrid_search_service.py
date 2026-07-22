"""Sequential, fail-closed composition of lexical, vector, RRF and Graph retrieval."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_hydration_repository import ContentHydrationRepository
from app.repositories.lexical_search_repository import LexicalSearchRepository
from app.services.graph_expansion_service import GraphExpansionService
from app.services.reciprocal_rank_fusion import FusedCandidate, RankedList, ReciprocalRankFusion
from app.services.vector_candidate_service import VectorCandidateService


@dataclass(frozen=True)
class HybridSearchSettings:
    """Internal candidate and fusion limits; never part of the public request."""

    lexical_candidate_k: int = 50
    vector_candidate_k: int = 50
    rrf_k: int = 60


@dataclass(frozen=True)
class _ComposedCandidate:
    """Minimal post-retrieval metadata retained until the single hydration call."""

    content_id: UUID
    matched_by: tuple[str, ...]


class HybridSearchService:
    """Orchestrate lexical, vector and Graph backfill without persistence or fallback."""

    def __init__(
        self,
        lexical_repository: LexicalSearchRepository,
        vector_candidates: VectorCandidateService,
        hydration_repository: ContentHydrationRepository,
        graph_expansion_service: GraphExpansionService,
        eligibility_repository: ContentEligibilityRepository,
        settings: HybridSearchSettings = HybridSearchSettings(),
    ) -> None:
        self._lexical_repository = lexical_repository
        self._vector_candidates = vector_candidates
        self._hydration_repository = hydration_repository
        self._graph_expansion_service = graph_expansion_service
        self._eligibility_repository = eligibility_repository
        self._settings = settings

    def search(self, query: str, top_k: int) -> dict[str, object]:
        """Run retrieval, Graph backfill and one final ordered hydration."""

        normalized_query = query.strip()
        lexical_limit = max(self._settings.lexical_candidate_k, top_k)
        vector_limit = max(self._settings.vector_candidate_k, top_k)
        try:
            lexical = self._lexical_repository.search(normalized_query, lexical_limit)
            vector = self._vector_candidates.search(normalized_query, vector_limit, -1.0)
        except SQLAlchemyError as error:
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        fused = ReciprocalRankFusion.fuse(
            [RankedList(origin="lexical", candidates=lexical), RankedList(origin="vector", candidates=vector)],
            top_k=top_k,
            rrf_k=self._settings.rrf_k,
        )
        try:
            candidates = self._compose_candidates(fused, top_k)
            hydrated = self._hydration_repository.hydrate([candidate.content_id for candidate in candidates])
        except SQLAlchemyError as error:
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        matched_by = {candidate.content_id: candidate.matched_by for candidate in candidates}
        items = [
            {
                "rank": rank,
                "content_id": content.content_id,
                "title": content.title,
                "url": content.url,
                "summary": content.summary,
                "matched_by": list(matched_by[content.content_id]),
            }
            for rank, content in enumerate(hydrated, start=1)
        ]
        return {"items": items, "total": len(items)}

    def _compose_candidates(
        self, fused: list[FusedCandidate], top_k: int
    ) -> list[_ComposedCandidate]:
        """Merge ordered RRF seeds with Graph backfill before eligibility and hydration."""

        seed_ids = [candidate.content_id for candidate in fused]
        graph_candidates = self._graph_expansion_service.expand(seed_ids)
        combined: list[_ComposedCandidate] = []
        seen_ids: set[UUID] = set()
        for candidate in fused:
            if candidate.content_id not in seen_ids:
                seen_ids.add(candidate.content_id)
                combined.append(_ComposedCandidate(candidate.content_id, candidate.matched_by))
        for candidate in graph_candidates:
            if candidate.content_id not in seen_ids:
                seen_ids.add(candidate.content_id)
                combined.append(_ComposedCandidate(candidate.content_id, ("graph",)))

        candidates_by_id = {candidate.content_id: candidate for candidate in combined}
        eligible_ids = self._eligibility_repository.filter_eligible([candidate.content_id for candidate in combined])
        return [candidates_by_id[content_id] for content_id in eligible_ids[:top_k]]

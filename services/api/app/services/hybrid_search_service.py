"""Sequential, fail-closed composition of lexical, vector and RRF retrieval."""

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from app.repositories.content_hydration_repository import ContentHydrationRepository
from app.repositories.lexical_search_repository import LexicalSearchRepository
from app.services.reciprocal_rank_fusion import RankedList, ReciprocalRankFusion
from app.services.vector_candidate_service import VectorCandidateService


@dataclass(frozen=True)
class HybridSearchSettings:
    """Internal candidate and fusion limits; never part of the public request."""

    lexical_candidate_k: int = 50
    vector_candidate_k: int = 50
    rrf_k: int = 60


class HybridSearchService:
    """Orchestrate exact lexical and vector retrieval without persistence or fallback."""

    def __init__(
        self,
        lexical_repository: LexicalSearchRepository,
        vector_candidates: VectorCandidateService,
        hydration_repository: ContentHydrationRepository,
        settings: HybridSearchSettings = HybridSearchSettings(),
    ) -> None:
        self._lexical_repository = lexical_repository
        self._vector_candidates = vector_candidates
        self._hydration_repository = hydration_repository
        self._settings = settings

    def search(self, query: str, top_k: int) -> dict[str, object]:
        """Run both strategies sequentially, fuse all candidates, then hydrate once."""

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
            hydrated = self._hydration_repository.hydrate([candidate.content_id for candidate in fused])
        except SQLAlchemyError as error:
            raise RuntimeError("Hybrid retrieval dependency unavailable") from error
        matched_by = {candidate.content_id: candidate.matched_by for candidate in fused}
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

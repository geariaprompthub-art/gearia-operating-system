"""Explicit, overridable dependency assembly for hybrid retrieval."""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.repositories.content_hydration_repository import ContentHydrationRepository
from app.repositories.lexical_search_repository import LexicalSearchRepository
from app.repositories.vector_search_repository import VectorSearchRepository
from app.services.embedding_dependencies import get_embedding_provider
from app.services.hybrid_search_service import HybridSearchService, HybridSearchSettings
from app.services.vector_candidate_service import VectorCandidateService


def get_hybrid_search_service(
    database: Session = Depends(get_db), provider: object = Depends(get_embedding_provider)
) -> HybridSearchService:
    """Build the sequential hybrid service only when its HTTP endpoint is invoked."""

    settings = get_settings()
    vector_repository = VectorSearchRepository(database)
    return HybridSearchService(
        lexical_repository=LexicalSearchRepository(database),
        vector_candidates=VectorCandidateService(vector_repository, provider),  # type: ignore[arg-type]
        hydration_repository=ContentHydrationRepository(database),
        settings=HybridSearchSettings(
            lexical_candidate_k=settings.hybrid_lexical_candidate_k,
            vector_candidate_k=settings.hybrid_vector_candidate_k,
            rrf_k=settings.hybrid_rrf_k,
        ),
    )

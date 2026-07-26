"""Explicit, overridable dependency assembly for hybrid retrieval."""

from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.prometheus_hybrid_search_telemetry import PrometheusHybridSearchTelemetry
from app.db import get_db
from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_hydration_repository import ContentHydrationRepository
from app.repositories.content_relationship_repository import ContentRelationshipRepository
from app.repositories.lexical_search_repository import LexicalSearchRepository
from app.repositories.rerank_document_repository import RerankDocumentRepository
from app.repositories.vector_search_repository import VectorSearchRepository
from app.services.embedding_dependencies import get_embedding_provider
from app.services.graph_candidate_aggregator import GraphCandidateAggregator
from app.services.graph_expansion_service import GraphExpansionService
from app.services.hybrid_search_service import HybridSearchService, HybridSearchSettings
from app.services.hybrid_search_telemetry import HybridSearchTelemetry, NoOpHybridSearchTelemetry
from app.services.hybrid_reranking_pipeline import HybridRerankingPipeline
from app.services.pre_reranking_candidate_pool import PreRerankingCandidatePool
from app.services.rerank_document_formatter import RerankDocumentFormatter
from app.services.reranking_service import RerankingService
from app.services.vector_candidate_service import VectorCandidateService
from app.services.voyage_reranking_provider import VoyageRerankingProvider


@lru_cache
def get_hybrid_search_telemetry() -> HybridSearchTelemetry:
    """Create the application-scoped adapter while keeping Prometheus out of services."""

    return PrometheusHybridSearchTelemetry() if get_settings().hybrid_search_telemetry_enabled else NoOpHybridSearchTelemetry()


def get_hybrid_search_service(
    database: Session = Depends(get_db),
    provider: object = Depends(get_embedding_provider),
    telemetry: HybridSearchTelemetry = Depends(get_hybrid_search_telemetry),
) -> HybridSearchService:
    """Build the sequential hybrid service only when its HTTP endpoint is invoked."""

    settings = get_settings()
    vector_repository = VectorSearchRepository(database)
    relationship_repository = ContentRelationshipRepository(database)
    graph_expansion_service = GraphExpansionService(
        relationship_repository=relationship_repository,
        candidate_aggregator=GraphCandidateAggregator(),
        max_seeds=20,
        candidate_limit=100,
    )
    reranking_provider = VoyageRerankingProvider(
        api_key=settings.voyage_api_key,
        model=settings.voyage_rerank_model,
        timeout_seconds=settings.voyage_rerank_timeout_seconds,
    )
    reranking_pipeline = HybridRerankingPipeline(
        eligibility_repository=ContentEligibilityRepository(database),
        rerank_document_repository=RerankDocumentRepository(database),
        formatter=RerankDocumentFormatter(),
        reranking_service=RerankingService(reranking_provider),
        hydration_repository=ContentHydrationRepository(database),
        candidate_pool=PreRerankingCandidatePool,
        telemetry=telemetry,
    )
    return HybridSearchService(
        lexical_repository=LexicalSearchRepository(database),
        vector_candidates=VectorCandidateService(vector_repository, provider),  # type: ignore[arg-type]
        graph_expansion_service=graph_expansion_service,
        reranking_pipeline=reranking_pipeline,
        telemetry=telemetry,
        settings=HybridSearchSettings(
            lexical_candidate_k=settings.hybrid_lexical_candidate_k,
            vector_candidate_k=settings.hybrid_vector_candidate_k,
            rrf_k=settings.hybrid_rrf_k,
        ),
    )

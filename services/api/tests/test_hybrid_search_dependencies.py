"""Construction tests for explicit Graph dependencies in hybrid retrieval."""

from sqlalchemy import event

from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_relationship_repository import ContentRelationshipRepository
from app.services.fake_embedding_provider import FakeEmbeddingProvider
from app.services.graph_candidate_aggregator import GraphCandidateAggregator
from app.services.graph_expansion_service import GraphExpansionService
from app.services.hybrid_search_dependencies import get_hybrid_search_service
from app.services.hybrid_search_service import HybridSearchService
from test_main import TestingSessionLocal


def test_hybrid_factory_injects_graph_dependencies_without_queries_or_provider_calls() -> None:
    """Factory construction is lazy and shares the request database session everywhere."""

    database = TestingSessionLocal()
    provider = FakeEmbeddingProvider()
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(database.bind, "before_cursor_execute", listener)
    try:
        service = get_hybrid_search_service(database=database, provider=provider)
        graph_service = service._graph_expansion_service
        assert isinstance(service, HybridSearchService)
        assert isinstance(graph_service, GraphExpansionService)
        assert isinstance(graph_service._relationship_repository, ContentRelationshipRepository)
        assert isinstance(graph_service._candidate_aggregator, GraphCandidateAggregator)
        assert isinstance(service._eligibility_repository, ContentEligibilityRepository)
        assert graph_service._relationship_repository._database is database
        assert service._eligibility_repository._database is database
        assert provider.calls == 0 and statements == []
    finally:
        event.remove(database.bind, "before_cursor_execute", listener)
        database.close()

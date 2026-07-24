"""TDD coverage for Sprint 12 pipeline delegation and candidate-boundary contracts."""

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.repositories.content_hydration_repository import HydratedContent
from app.repositories.lexical_search_repository import LexicalSearchCandidate
from app.repositories.rerank_document_repository import RerankDocumentRecord
from app.repositories.vector_search_repository import VectorSearchCandidate
from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.hybrid_reranking_pipeline import HybridRerankingPipeline
from app.services.hybrid_search_service import HybridSearchService, HybridSearchSettings
from app.services.pre_reranking_candidate_pool import PreRerankingCandidatePool
from app.services.rerank_document_formatter import RerankDocumentFormatter
from app.services.reranking_contracts import ProviderRerankResult, RerankCandidate
from app.services.reranking_service import RerankingService


class Lexical:
    def __init__(self, values: list[LexicalSearchCandidate]) -> None:
        self.values = values
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int) -> list[LexicalSearchCandidate]:
        self.calls.append((query, limit))
        return self.values


class Vector:
    def __init__(self, values: list[VectorSearchCandidate]) -> None:
        self.values = values
        self.calls: list[tuple[str, int, float]] = []

    def search(self, query: str, limit: int, threshold: float) -> list[VectorSearchCandidate]:
        self.calls.append((query, limit, threshold))
        return self.values


class Graph:
    def __init__(self, values: list[GraphExpandedCandidate]) -> None:
        self.values = values
        self.calls: list[list[UUID]] = []

    def expand(self, content_ids: list[UUID]) -> list[GraphExpandedCandidate]:
        self.calls.append(list(content_ids))
        return self.values


class Pipeline:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[str, list[object], list[object], int, int]] = []

    def run(self, query: str, rrf_candidates: list[object], graph_candidates: list[object], top_k: int, candidate_limit: int) -> dict[str, object]:
        self.calls.append((query, rrf_candidates, graph_candidates, candidate_limit, top_k))
        return self.result


class FailingLexical(Lexical):
    def search(self, query: str, limit: int) -> list[LexicalSearchCandidate]:
        super().search(query, limit)
        raise OperationalError("select", {}, Exception("offline"))


class FailingGraph(Graph):
    def expand(self, content_ids: list[UUID]) -> list[GraphExpandedCandidate]:
        super().expand(content_ids)
        raise OperationalError("select", {}, Exception("offline"))


def test_hybrid_search_delegates_once_without_local_hydration_or_eligibility() -> None:
    lexical_id, vector_id, graph_id = uuid4(), uuid4(), uuid4()
    lexical = Lexical([LexicalSearchCandidate(lexical_id)])
    vector = Vector([VectorSearchCandidate(vector_id, 0.9)])
    graph = Graph([GraphExpandedCandidate(graph_id, Decimal("1"), (lexical_id,))])
    expected = {"items": [{"rank": 1, "content_id": graph_id}], "total": 1}
    pipeline = Pipeline(expected)

    service = HybridSearchService(
        lexical_repository=lexical,
        vector_candidates=vector,
        graph_expansion_service=graph,
        reranking_pipeline=pipeline,
        settings=HybridSearchSettings(lexical_candidate_k=4, vector_candidate_k=7),
    )

    assert service.search("  query  ", top_k=2) is expected
    assert lexical.calls == [("query", 4)]
    assert vector.calls == [("query", 7, -1.0)]
    assert len(graph.calls) == 1
    assert len(pipeline.calls) == 1
    query, fused, expanded, candidate_limit, top_k = pipeline.calls[0]
    assert query == "query"
    assert {candidate.content_id for candidate in fused} == {lexical_id, vector_id}
    assert expanded == graph.values
    assert candidate_limit == 11
    assert top_k == 2


def test_hybrid_search_preserves_lexical_vector_and_empty_source_contracts() -> None:
    """Lexical-only, vector-only, and empty retrieval remain valid service inputs."""

    lexical_id, vector_id = uuid4(), uuid4()
    scenarios = [
        ([LexicalSearchCandidate(lexical_id)], [], lexical_id),
        ([], [VectorSearchCandidate(vector_id, 0.9)], vector_id),
        ([], [], None),
    ]
    for lexical_values, vector_values, expected_id in scenarios:
        pipeline = Pipeline({"items": [], "total": 0})
        service = HybridSearchService(Lexical(lexical_values), Vector(vector_values), Graph([]), pipeline)

        assert service.search("query", top_k=1) == {"items": [], "total": 0}
        _, fused, _, candidate_limit, public_top_k = pipeline.calls[0]
        assert candidate_limit == 100
        assert public_top_k == 1
        if expected_id is None:
            assert fused == []
        else:
            assert [candidate.content_id for candidate in fused] == [expected_id]


def test_hybrid_search_retrieval_and_graph_fail_closed_before_pipeline() -> None:
    """Legacy service-level failure coverage remains at its retained boundary."""

    seed = uuid4()
    pipeline = Pipeline({"items": [], "total": 0})
    with pytest.raises(RuntimeError, match="dependency unavailable"):
        HybridSearchService(
            FailingLexical([]), Vector([]), Graph([]), pipeline
        ).search("query", top_k=1)
    assert pipeline.calls == []

    pipeline = Pipeline({"items": [], "total": 0})
    with pytest.raises(RuntimeError, match="dependency unavailable"):
        HybridSearchService(
            Lexical([LexicalSearchCandidate(seed)]), Vector([]), FailingGraph([]), pipeline
        ).search("query", top_k=1)
    assert pipeline.calls == []


class AllEligible:
    def filter_eligible(self, content_ids: Sequence[UUID]) -> list[UUID]:
        return list(content_ids)


class Documents:
    def hydrate(self, content_ids: Sequence[UUID]) -> list[RerankDocumentRecord]:
        return [
            RerankDocumentRecord(content_id, f"Title {content_id}", "Summary", None, (), ())
            for content_id in content_ids
        ]


class PublicHydration:
    def hydrate(self, content_ids: Sequence[UUID]) -> list[HydratedContent]:
        return [HydratedContent(content_id, f"Title {content_id}", f"https://test/{content_id}", "Summary") for content_id in content_ids]


class ReorderingProvider:
    def __init__(self, first_id: UUID) -> None:
        self.first_id = first_id
        self.calls: list[list[RerankCandidate]] = []

    def rerank(self, _: str, candidates: Sequence[RerankCandidate]) -> list[ProviderRerankResult]:
        values = list(candidates)
        self.calls.append(values)
        ordered = [candidate for candidate in values if candidate.content_id == self.first_id]
        ordered.extend(candidate for candidate in values if candidate.content_id != self.first_id)
        return [ProviderRerankResult(candidate.content_id, float(len(ordered) - index)) for index, candidate in enumerate(ordered)]


def test_disjoint_retrieval_union_reaches_provider_before_public_top_k() -> None:
    """A candidate beyond the former 50-item RRF cut can win after reranking."""

    lexical_ids = [uuid4() for _ in range(50)]
    vector_ids = [uuid4() for _ in range(50)]
    formerly_truncated = vector_ids[29]
    graph_id = uuid4()
    provider = ReorderingProvider(formerly_truncated)
    pipeline = HybridRerankingPipeline(
        eligibility_repository=AllEligible(),
        rerank_document_repository=Documents(),
        formatter=RerankDocumentFormatter(),
        reranking_service=RerankingService(provider),
        hydration_repository=PublicHydration(),
        candidate_pool=PreRerankingCandidatePool,
    )
    graph = Graph([GraphExpandedCandidate(graph_id, Decimal("1"), (lexical_ids[0],))])
    service = HybridSearchService(
        lexical_repository=Lexical([LexicalSearchCandidate(content_id) for content_id in lexical_ids]),
        vector_candidates=Vector([VectorSearchCandidate(content_id, 0.9) for content_id in vector_ids]),
        graph_expansion_service=graph,
        reranking_pipeline=pipeline,
        settings=HybridSearchSettings(lexical_candidate_k=50, vector_candidate_k=50),
    )

    result = service.search("query", top_k=1)

    assert len(provider.calls) == 1
    assert len(graph.calls) == 1
    assert len(graph.calls[0]) == 100
    assert len(provider.calls[0]) > 1
    assert formerly_truncated in {candidate.content_id for candidate in provider.calls[0]}
    assert result["total"] == 1
    assert result["items"][0]["content_id"] == formerly_truncated
    assert result["items"][0]["rank"] == 1

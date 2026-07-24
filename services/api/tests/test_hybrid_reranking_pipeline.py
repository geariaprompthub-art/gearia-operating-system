"""Offline orchestration coverage for the ADR-001 hybrid reranking boundary."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from math import inf, nan
from uuid import UUID, uuid4

import pytest

from app.repositories.content_hydration_repository import HydratedContent
from app.repositories.rerank_document_repository import RerankDocumentRecord
from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.hybrid_reranking_pipeline import (
    HybridRerankingPipeline,
    RerankingPipelineHydrationError,
)
from app.services.pre_reranking_candidate_pool import ConsolidatedPoolCandidate, PreRerankingCandidatePool
from app.services.reciprocal_rank_fusion import FusedCandidate
from app.services.rerank_document_formatter import RerankDocumentFormatter
from app.services.reranking_contracts import ProviderRerankResult, RerankCandidate
from app.services.reranking_provider_errors import (
    RerankingProviderConfigurationError,
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)
from app.services.reranking_service import RerankingService


class Eligibility:
    def __init__(self, eligible_ids: list[UUID] | None = None) -> None:
        self.eligible_ids = eligible_ids
        self.calls: list[list[UUID]] = []

    def filter_eligible(self, content_ids: Sequence[UUID]) -> list[UUID]:
        self.calls.append(list(content_ids))
        return list(content_ids if self.eligible_ids is None else self.eligible_ids)


class Documents:
    def __init__(self, records: list[RerankDocumentRecord]) -> None:
        self.records = records
        self.calls: list[list[UUID]] = []

    def hydrate(self, content_ids: Sequence[UUID]) -> list[RerankDocumentRecord]:
        self.calls.append(list(content_ids))
        return list(self.records)


class PublicHydration:
    def __init__(self, records: list[HydratedContent]) -> None:
        self.records = records
        self.calls: list[list[UUID]] = []

    def hydrate(self, content_ids: Sequence[UUID]) -> list[HydratedContent]:
        self.calls.append(list(content_ids))
        return list(self.records)


class Provider:
    def __init__(
        self,
        results: Sequence[ProviderRerankResult] = (),
        error: Exception | None = None,
    ) -> None:
        self.results = list(results)
        self.error = error
        self.calls: list[tuple[str, list[RerankCandidate]]] = []

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> list[ProviderRerankResult]:
        self.calls.append((query, list(candidates)))
        if self.error is not None:
            raise self.error
        return list(self.results)


class PoolSpy:
    configured: list[ConsolidatedPoolCandidate] = []
    calls: list[tuple[list[FusedCandidate], list[GraphExpandedCandidate], int]] = []

    @classmethod
    def reset(cls, configured: Sequence[ConsolidatedPoolCandidate]) -> None:
        cls.configured = list(configured)
        cls.calls = []

    @classmethod
    def build(
        cls,
        rrf_candidates: Sequence[FusedCandidate],
        graph_candidates: Sequence[GraphExpandedCandidate],
        top_k: int,
    ) -> list[ConsolidatedPoolCandidate]:
        cls.calls.append((list(rrf_candidates), list(graph_candidates), top_k))
        return list(cls.configured)


def _rrf(content_id: UUID, matched_by: tuple[str, ...] = ("lexical",)) -> FusedCandidate:
    return FusedCandidate(content_id, 1.0, matched_by)


def _graph(content_id: UUID) -> GraphExpandedCandidate:
    return GraphExpandedCandidate(content_id, Decimal("1"), (uuid4(),))


def _pool_candidate(
    content_id: UUID, matched_by: tuple[str, ...] = ("lexical",)
) -> ConsolidatedPoolCandidate:
    return ConsolidatedPoolCandidate(content_id, matched_by)


def _document(content_id: UUID, title: str | None = "Title") -> RerankDocumentRecord:
    return RerankDocumentRecord(content_id, title, "Summary", "Category", ("topic",), ("keyword",))


def _public(content_id: UUID) -> HydratedContent:
    return HydratedContent(content_id, "Title", f"https://test/{content_id}", "Summary")


def _pipeline(
    provider: Provider,
    documents: list[RerankDocumentRecord],
    public: list[HydratedContent],
    eligible_ids: list[UUID] | None = None,
    candidate_pool: type[PreRerankingCandidatePool] = PoolSpy,
) -> tuple[HybridRerankingPipeline, Eligibility, Documents, PublicHydration]:
    eligibility = Eligibility(eligible_ids)
    document_repository = Documents(documents)
    hydration = PublicHydration(public)
    return (
        HybridRerankingPipeline(
            eligibility,
            document_repository,
            RerankDocumentFormatter(),
            RerankingService(provider),
            hydration,
            candidate_pool,
        ),
        eligibility,
        document_repository,
        hydration,
    )


def test_pipeline_uses_pool_once_sends_all_selected_candidates_and_applies_top_k_last() -> None:
    first, second, graph = uuid4(), uuid4(), uuid4()
    PoolSpy.reset(
        [
            _pool_candidate(first, ("lexical", "vector")),
            _pool_candidate(second, ("vector",)),
            _pool_candidate(graph, ("graph",)),
        ]
    )
    provider = Provider(
        [
            ProviderRerankResult(graph, 0.1),
            ProviderRerankResult(first, 0.9),
            ProviderRerankResult(second, 0.5),
        ]
    )
    pipeline, eligibility, documents, hydration = _pipeline(
        provider,
        [_document(graph), _document(first), _document(second)],
        [_public(first), _public(graph)],
    )
    rrf = [_rrf(first, ("lexical", "vector")), _rrf(second, ("vector",))]
    graphs = [_graph(graph)]

    result = pipeline.run("  mixed query  ", rrf, graphs, top_k=2, candidate_limit=2)

    assert PoolSpy.calls == [(rrf, graphs, 2)]
    assert eligibility.calls == [[first, second, graph]]
    assert documents.calls == [[first, second, graph]]
    assert provider.calls == [
        (
            "  mixed query  ",
            [
                RerankCandidate(first, provider.calls[0][1][0].document_text, 1, ("lexical", "vector")),
                RerankCandidate(second, provider.calls[0][1][1].document_text, 2, ("vector",)),
                RerankCandidate(graph, provider.calls[0][1][2].document_text, 3, ("graph",)),
            ],
        )
    ]
    assert hydration.calls == [[graph, first]]
    assert [item["content_id"] for item in result["items"]] == [graph, first]
    assert [item["rank"] for item in result["items"]] == [1, 2]
    assert [item["matched_by"] for item in result["items"]] == [["graph"], ["lexical", "vector"]]
    assert result["total"] == 2
    assert all("rerank_score" not in item for item in result["items"])


def test_small_candidate_limit_bounds_pool_and_provider_independently_of_public_top_k() -> None:
    """Eligible candidates beyond H never reach the provider, regardless of public top_k."""

    rrf_ids = [uuid4() for _ in range(30)]
    graph_ids = [uuid4() for _ in range(30)]
    rrf = [_rrf(content_id) for content_id in rrf_ids]
    graphs = [_graph(content_id) for content_id in graph_ids]
    selected = PreRerankingCandidatePool.build(rrf, graphs, top_k=1)
    selected_ids = [candidate.content_id for candidate in selected]

    assert PreRerankingCandidatePool.horizon(1) == 20
    assert len(rrf) + len(graphs) > len(selected_ids) == 20

    providers: list[Provider] = []
    for public_top_k in (1, 10):
        provider = Provider(
            [ProviderRerankResult(content_id, float(20 - index)) for index, content_id in enumerate(selected_ids)]
        )
        providers.append(provider)
        pipeline, eligibility, documents, _ = _pipeline(
            provider,
            [_document(content_id) for content_id in selected_ids],
            [_public(content_id) for content_id in selected_ids[:public_top_k]],
            candidate_pool=PreRerankingCandidatePool,
        )

        result = pipeline.run(
            "query",
            rrf,
            graphs,
            top_k=public_top_k,
            candidate_limit=1,
        )

        assert eligibility.calls == [selected_ids]
        assert documents.calls == [selected_ids]
        assert [candidate.content_id for candidate in provider.calls[0][1]] == selected_ids
        assert len(provider.calls[0][1]) == PreRerankingCandidatePool.horizon(1)
        assert result["total"] == public_top_k

    assert [len(provider.calls[0][1]) for provider in providers] == [20, 20]


def test_pipeline_requires_an_explicit_provider_safe_candidate_limit() -> None:
    """Public top_k cannot silently become the pre-reranking horizon."""

    content_id = uuid4()
    provider = Provider([ProviderRerankResult(content_id, 1.0)])
    pipeline, _, _, _ = _pipeline(provider, [_document(content_id)], [_public(content_id)])
    PoolSpy.reset([_pool_candidate(content_id)])

    with pytest.raises(TypeError):
        pipeline.run("query", [_rrf(content_id)], [], top_k=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="candidate_limit"):
        pipeline.run("query", [_rrf(content_id)], [], top_k=1, candidate_limit=101)


def test_pipeline_reconciles_equal_document_text_by_content_id_and_provider_order() -> None:
    first, second = uuid4(), uuid4()
    PoolSpy.reset([_pool_candidate(first), _pool_candidate(second, ("graph",))])
    provider = Provider([ProviderRerankResult(second, 0.0), ProviderRerankResult(first, 1.0)])
    pipeline, _, _, hydration = _pipeline(
        provider,
        [_document(first, "Same"), _document(second, "Same")],
        [_public(second), _public(first)],
    )

    result = pipeline.run("query", [_rrf(first)], [_graph(second)], top_k=2, candidate_limit=2)

    assert hydration.calls == [[second, first]]
    assert [item["content_id"] for item in result["items"]] == [second, first]
    assert [item["matched_by"] for item in result["items"]] == [["graph"], ["lexical"]]


@pytest.mark.parametrize(
    "results",
    [
        lambda first, second: [ProviderRerankResult(uuid4(), 0.5), ProviderRerankResult(second, 0.1)],
        lambda first, second: [ProviderRerankResult(first, 0.5), ProviderRerankResult(first, 0.1)],
        lambda first, second: [ProviderRerankResult(first, 0.5)],
        lambda first, second: [ProviderRerankResult(first, 0.5), ProviderRerankResult(second, 0.1), ProviderRerankResult(uuid4(), 0.0)],
        lambda first, second: [ProviderRerankResult(first, nan), ProviderRerankResult(second, 0.1)],
        lambda first, second: [ProviderRerankResult(first, inf), ProviderRerankResult(second, 0.1)],
    ],
)
def test_invalid_provider_exchange_fails_closed_without_public_hydration(results: object) -> None:
    first, second = uuid4(), uuid4()
    PoolSpy.reset([_pool_candidate(first), _pool_candidate(second)])
    provider = Provider(results(first, second))  # type: ignore[operator]
    pipeline, _, _, hydration = _pipeline(provider, [_document(first), _document(second)], [_public(first), _public(second)])

    with pytest.raises(RerankingProviderResponseError):
        pipeline.run("query", [_rrf(first), _rrf(second)], [], top_k=2, candidate_limit=2)

    assert len(provider.calls) == 1
    assert hydration.calls == []


@pytest.mark.parametrize(
    "error",
    [
        RerankingProviderConfigurationError("configuration"),
        RerankingProviderUnavailableError("unavailable"),
        RerankingProviderResponseError("invalid response"),
        RuntimeError("unexpected"),
    ],
)
def test_provider_errors_propagate_once_without_fallback(error: Exception) -> None:
    content_id = uuid4()
    PoolSpy.reset([_pool_candidate(content_id)])
    provider = Provider(error=error)
    pipeline, _, _, hydration = _pipeline(provider, [_document(content_id)], [_public(content_id)])

    with pytest.raises(type(error)):
        pipeline.run("query", [_rrf(content_id)], [], top_k=1, candidate_limit=1)

    assert len(provider.calls) == 1
    assert hydration.calls == []


def test_empty_pool_skips_provider_and_all_hydration() -> None:
    PoolSpy.reset([])
    provider = Provider()
    pipeline, eligibility, documents, hydration = _pipeline(provider, [], [])

    assert pipeline.run("query", [], [], top_k=1, candidate_limit=1) == {"items": [], "total": 0}
    assert eligibility.calls == [[]]
    assert documents.calls == []
    assert provider.calls == []
    assert hydration.calls == []


def test_no_eligible_candidates_skip_provider_after_pool_selection() -> None:
    content_id = uuid4()
    PoolSpy.reset([_pool_candidate(content_id)])
    provider = Provider()
    pipeline, eligibility, documents, hydration = _pipeline(provider, [_document(content_id)], [_public(content_id)], [])

    assert pipeline.run("query", [_rrf(content_id)], [], top_k=1, candidate_limit=1) == {"items": [], "total": 0}
    assert eligibility.calls == [[content_id]]
    assert documents.calls == provider.calls == hydration.calls == []


def test_missing_document_hydration_fails_closed_before_provider() -> None:
    first, missing, last = uuid4(), uuid4(), uuid4()
    PoolSpy.reset([_pool_candidate(first), _pool_candidate(missing), _pool_candidate(last, ("graph",))])
    provider = Provider([ProviderRerankResult(last, 0.9), ProviderRerankResult(first, 0.1)])
    pipeline, _, documents, hydration = _pipeline(
        provider,
        [_document(first), _document(last)],
        [_public(last)],
    )

    with pytest.raises(RerankingPipelineHydrationError):
        pipeline.run("query", [_rrf(first)], [_graph(missing), _graph(last)], top_k=3, candidate_limit=3)

    assert documents.calls == [[first, missing, last]]
    assert provider.calls == []
    assert hydration.calls == []


@pytest.mark.parametrize(
    "records_factory",
    [
        lambda first, second: [_document(first)],
        lambda first, second: [_document(first), _document(first)],
        lambda first, second: [_document(first), _document(uuid4())],
        lambda first, second: [_document(first), _document(second), _document(uuid4())],
        lambda first, second: [object(), _document(second)],
    ],
)
def test_invalid_document_hydration_never_sends_a_subset_to_provider(
    records_factory: object,
) -> None:
    first, second = uuid4(), uuid4()
    PoolSpy.reset([_pool_candidate(first), _pool_candidate(second)])
    provider = Provider(
        [ProviderRerankResult(first, 0.8), ProviderRerankResult(second, 0.7)]
    )
    records = records_factory(first, second)  # type: ignore[operator]
    pipeline, _, documents, hydration = _pipeline(provider, records, [_public(first), _public(second)])

    with pytest.raises(RerankingPipelineHydrationError):
        pipeline.run("query", [_rrf(first), _rrf(second)], [], top_k=2, candidate_limit=2)

    assert documents.calls == [[first, second]]
    assert provider.calls == []
    assert hydration.calls == []


@pytest.mark.parametrize(
    "contents_factory",
    [
        lambda first, second: [_public(first)],
        lambda first, second: [_public(first), _public(first)],
        lambda first, second: [_public(first), _public(uuid4())],
        lambda first, second: [_public(first), _public(second), _public(uuid4())],
        lambda first, second: [object(), _public(second)],
    ],
)
def test_invalid_public_hydration_fails_closed_without_reordering_or_backfill(
    contents_factory: object,
) -> None:
    first, second = uuid4(), uuid4()
    PoolSpy.reset([_pool_candidate(first), _pool_candidate(second)])
    provider = Provider(
        [ProviderRerankResult(second, 0.2), ProviderRerankResult(first, 0.9)]
    )
    contents = contents_factory(first, second)  # type: ignore[operator]
    pipeline, _, _, hydration = _pipeline(
        provider,
        [_document(first), _document(second)],
        contents,
    )

    with pytest.raises(RerankingPipelineHydrationError):
        pipeline.run("query", [_rrf(first), _rrf(second)], [], top_k=2, candidate_limit=2)

    assert len(provider.calls) == 1
    assert hydration.calls == [[second, first]]


def test_pool_receives_distinct_rrf_and_graph_sequences_without_local_deduplication() -> None:
    shared, graph_only = uuid4(), uuid4()
    PoolSpy.reset([_pool_candidate(shared, ("lexical", "vector")), _pool_candidate(graph_only, ("graph",))])
    provider = Provider([ProviderRerankResult(shared, 0.4), ProviderRerankResult(graph_only, 0.3)])
    pipeline, _, _, _ = _pipeline(provider, [_document(shared), _document(graph_only)], [_public(shared), _public(graph_only)])
    rrf = [_rrf(shared, ("lexical", "vector"))]
    graphs = [_graph(shared), _graph(graph_only)]

    pipeline.run("query", rrf, graphs, top_k=2, candidate_limit=2)

    assert PoolSpy.calls == [(rrf, graphs, 2)]
    assert [candidate.content_id for candidate in provider.calls[0][1]] == [shared, graph_only]

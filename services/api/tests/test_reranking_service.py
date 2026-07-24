"""Pure unit coverage for the provider-agnostic reranking service."""

from collections.abc import Sequence
from math import inf, nan
from uuid import UUID, uuid4

import pytest

from app.services.reranking_contracts import ProviderRerankResult, RerankCandidate, RerankedCandidate
from app.services.reranking_provider_errors import RerankingProviderResponseError
from app.services.reranking_service import RerankingService


class SpyProvider:
    """Configurable in-memory provider that records only calls and supplied candidates."""

    def __init__(self, results: object = ()) -> None:
        self.results = results
        self.calls: list[tuple[str, list[RerankCandidate]]] = []
        self.error: Exception | None = None

    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> object:
        self.calls.append((query, list(candidates)))
        if self.error is not None:
            raise self.error
        return self.results


def _candidate(
    content_id: UUID | None = None,
    rank: int = 1,
    matched_by: tuple[str, ...] = ("lexical",),
    document_text: str = "Document",
) -> RerankCandidate:
    return RerankCandidate(content_id or uuid4(), document_text, rank, matched_by)


def _results(*candidates: RerankCandidate, scores: Sequence[float] | None = None) -> list[ProviderRerankResult]:
    return [
        ProviderRerankResult(candidate.content_id, (scores or [float(index) for index in range(len(candidates))])[index])
        for index, candidate in enumerate(candidates)
    ]


def test_empty_candidates_skip_provider_and_return_empty_result() -> None:
    provider = SpyProvider()
    assert RerankingService(provider).rerank("query", []) == []
    assert provider.calls == []


def test_reranks_one_candidate_and_preserves_immutable_provenance() -> None:
    candidate = _candidate(matched_by=("lexical", "vector"))
    provider = SpyProvider([ProviderRerankResult(candidate.content_id, 1)])

    result = RerankingService(provider).rerank("query", [candidate])

    assert result == [RerankedCandidate(candidate.content_id, candidate.pre_rerank_rank, candidate.matched_by, 1.0)]
    assert provider.calls == [("query", [candidate])]
    with pytest.raises((AttributeError, TypeError)):
        result[0].pre_rerank_rank = 2  # type: ignore[misc]
def test_reranks_single_and_multiple_candidates_without_exposing_scores() -> None:
    first, second, third = _candidate(rank=1), _candidate(rank=2, matched_by=("vector",)), _candidate(rank=3, matched_by=("lexical", "graph"))
    provider = SpyProvider(_results(third, first, second, scores=[0.2, 0.9, 0.7]))

    result = RerankingService(provider).rerank("  query  ", [first, second, third])

    assert [item.content_id for item in result] == [third.content_id, first.content_id, second.content_id]
    assert [item.pre_rerank_rank for item in result] == [3, 1, 2]
    assert [item.matched_by for item in result] == [third.matched_by, first.matched_by, second.matched_by]
    assert [item.rerank_score for item in result] == [0.2, 0.9, 0.7]
    assert provider.calls == [("  query  ", [first, second, third])]
    assert all(not hasattr(item, "score") for item in result)


def test_provider_order_is_authoritative_even_when_scores_are_not_descending() -> None:
    first = _candidate(rank=1)
    second = _candidate(rank=2)
    provider = SpyProvider(_results(second, first, scores=[0.1, 0.9]))

    result = RerankingService(provider).rerank("query", [first, second])

    assert [item.content_id for item in result] == [second.content_id, first.content_id]
    assert [item.rerank_score for item in result] == [0.1, 0.9]


def test_repeated_calls_are_deterministic_and_do_not_mutate_input() -> None:
    first, second = _candidate(rank=1), _candidate(rank=2)
    candidates = [first, second]
    original = list(candidates)
    provider = SpyProvider(_results(second, first, scores=[0.8, 0.2]))
    service = RerankingService(provider)

    first_result = service.rerank("query", candidates)
    second_result = service.rerank("query", candidates)

    assert first_result == second_result
    assert candidates == original
    assert provider.calls == [("query", original), ("query", original)]


@pytest.mark.parametrize("query", [None, 1, b"query", "", "   "])
def test_rejects_invalid_query_before_provider(query: object) -> None:
    provider = SpyProvider()
    with pytest.raises(ValueError):
        RerankingService(provider).rerank(query, [_candidate()])  # type: ignore[arg-type]
    assert provider.calls == []


@pytest.mark.parametrize(
    "candidates",
    [
        None,
        1,
        "candidate",
        b"candidate",
        [object()],
        [_candidate(document_text=" ")],
        [RerankCandidate("not-a-uuid", "text", 1, ("lexical",))],
        [_candidate(rank=0)],
        [_candidate(rank=True)],
        [RerankCandidate(uuid4(), "text", 1, ["lexical"])],
        [_candidate(matched_by=())],
        [_candidate(matched_by=("unknown",))],
        [_candidate(matched_by=("vector", "lexical"))],
        [_candidate(matched_by=("lexical", "lexical"))],
        [RerankCandidate(uuid4(), 1, 1, ("lexical",))],
        (lambda candidate: [candidate, RerankCandidate(candidate.content_id, "second", 2, ("vector",))])(_candidate()),
        (lambda first, second: [first, second])(_candidate(rank=1), _candidate(rank=1)),
        [_candidate(rank=index + 1) for index in range(101)],
    ],
)
def test_rejects_invalid_candidates_before_provider(candidates: object) -> None:
    provider = SpyProvider()
    with pytest.raises(ValueError):
        RerankingService(provider).rerank("query", candidates)  # type: ignore[arg-type]
    assert provider.calls == []


@pytest.mark.parametrize(
    "provider_results",
    [
        None,
        1,
        "result",
        b"result",
        [object()],
        [ProviderRerankResult(uuid4(), 1.0)],
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, 1.0)] * 2,
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, 1.0)],
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, True), ProviderRerankResult(candidates[1].content_id, 0.5)],
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, "0.5"), ProviderRerankResult(candidates[1].content_id, 0.5)],
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, nan), ProviderRerankResult(candidates[1].content_id, 0.5)],
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, inf), ProviderRerankResult(candidates[1].content_id, 0.5)],
        lambda candidates: [ProviderRerankResult(candidates[0].content_id, -inf), ProviderRerankResult(candidates[1].content_id, 0.5)],
    ],
)
def test_rejects_invalid_provider_responses_without_partial_output(provider_results: object) -> None:
    candidates = [_candidate(rank=1), _candidate(rank=2)]
    results = provider_results(candidates) if callable(provider_results) else provider_results
    provider = SpyProvider(results)
    with pytest.raises(RerankingProviderResponseError):
        RerankingService(provider).rerank("query", candidates)
    assert len(provider.calls) == 1


def test_rejects_missing_extra_unknown_and_duplicate_provider_ids() -> None:
    first, second = _candidate(rank=1), _candidate(rank=2)
    scenarios = [
        [ProviderRerankResult(first.content_id, 1.0)],
        [ProviderRerankResult(first.content_id, 1.0), ProviderRerankResult(second.content_id, 0.5), ProviderRerankResult(uuid4(), 0.1)],
        [ProviderRerankResult(first.content_id, 1.0), ProviderRerankResult(first.content_id, 0.5)],
        [ProviderRerankResult(uuid4(), 1.0), ProviderRerankResult(second.content_id, 0.5)],
    ]
    for results in scenarios:
        provider = SpyProvider(results)
        with pytest.raises(RerankingProviderResponseError):
            RerankingService(provider).rerank("query", [first, second])
        assert len(provider.calls) == 1


def test_provider_exception_propagates_without_partial_output_or_retry() -> None:
    provider = SpyProvider()
    provider.error = RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        RerankingService(provider).rerank("query", [_candidate()])
    assert len(provider.calls) == 1

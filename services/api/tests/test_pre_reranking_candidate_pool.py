"""Unit coverage for the ADR-001 pre-reranking candidate pool."""

from decimal import Decimal
from uuid import UUID

import pytest

from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.pre_reranking_candidate_pool import PreRerankingCandidatePool
from app.services.reciprocal_rank_fusion import FusedCandidate


def _rrf(number: int, matched_by: tuple[str, ...] = ("lexical",)) -> FusedCandidate:
    return FusedCandidate(UUID(int=number), 1.0, matched_by)


def _graph(number: int) -> GraphExpandedCandidate:
    return GraphExpandedCandidate(UUID(int=number), Decimal("1"), (UUID(int=999),))


def _ids(values: list[object]) -> list[UUID]:
    return [value.content_id for value in values]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("top_k", "expected_horizon", "rrf_budget", "graph_budget"),
    [(1, 20, 16, 4), (10, 50, 40, 10), (20, 100, 80, 20), (100, 100, 80, 20)],
)
def test_horizon_and_source_budgets_follow_accepted_adr(
    top_k: int, expected_horizon: int, rrf_budget: int, graph_budget: int
) -> None:
    rrf = [_rrf(index) for index in range(1, 101)]
    graph = [_graph(index) for index in range(101, 201)]

    result = PreRerankingCandidatePool.build(rrf, graph, top_k)

    assert PreRerankingCandidatePool.horizon(top_k) == expected_horizon
    assert _ids(result) == [UUID(int=index) for index in range(1, rrf_budget + 1)] + [
        UUID(int=index) for index in range(101, 101 + graph_budget)
    ]


def test_graph_shortage_is_filled_by_remaining_rrf_in_rrf_order() -> None:
    result = PreRerankingCandidatePool.build(
        [_rrf(index) for index in range(1, 31)], [_graph(101), _graph(102)], top_k=1
    )

    assert _ids(result) == [UUID(int=index) for index in range(1, 17)] + [UUID(int=101), UUID(int=102)] + [
        UUID(int=index) for index in range(17, 19)
    ]


def test_rrf_shortage_is_filled_by_remaining_graph_in_graph_order() -> None:
    result = PreRerankingCandidatePool.build(
        [_rrf(1), _rrf(2)], [_graph(index) for index in range(101, 121)], top_k=1
    )

    assert _ids(result) == [UUID(int=1), UUID(int=2)] + [UUID(int=index) for index in range(101, 119)]


def test_graph_duplicates_of_rrf_and_multiple_duplicates_do_not_consume_graph_budget() -> None:
    result = PreRerankingCandidatePool.build(
        [_rrf(1), _rrf(1, ("vector",)), _rrf(2)],
        [_graph(1), _graph(3), _graph(3), _graph(4), _graph(4), _graph(5), _graph(6)],
        top_k=1,
    )

    assert _ids(result) == [UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4), UUID(int=5), UUID(int=6)]
    assert result[0].matched_by == ("lexical",)
    assert all(candidate.matched_by == ("graph",) for candidate in result[2:])


def test_pool_preserves_source_order_and_never_exceeds_horizon_or_absolute_cap() -> None:
    result = PreRerankingCandidatePool.build(
        [_rrf(index) for index in range(1, 151)], [_graph(index) for index in range(151, 301)], top_k=100
    )

    assert len(result) == 100
    assert len(result) <= PreRerankingCandidatePool.horizon(100) <= 100
    assert _ids(result) == [UUID(int=index) for index in range(1, 81)] + [
        UUID(int=index) for index in range(151, 171)
    ]


def test_returns_only_available_candidates_without_artificial_backfill() -> None:
    result = PreRerankingCandidatePool.build([_rrf(1)], [_graph(2)], top_k=10)

    assert _ids(result) == [UUID(int=1), UUID(int=2)]


@pytest.mark.parametrize("top_k", [0, -1, True, 1.0])
def test_rejects_invalid_top_k(top_k: object) -> None:
    with pytest.raises(ValueError, match="top_k"):
        PreRerankingCandidatePool.build([], [], top_k)  # type: ignore[arg-type]

"""Unit tests for pure Decimal-based one-hop graph aggregation."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.repositories.content_relationship_repository import RelationshipNeighbor
from app.services.graph_candidate_aggregator import (
    GraphCandidateAggregator,
    GraphExpandedCandidate,
    GraphSeed,
)


def _seed(content_id: UUID, rank: int) -> GraphSeed:
    """Create a concise seed fixture."""

    return GraphSeed(content_id=content_id, seed_rank=rank)


def _neighbor(seed_id: UUID, neighbor_id: UUID, score: Decimal = Decimal("100")) -> RelationshipNeighbor:
    """Create a valid relationship-neighbor fixture."""

    return RelationshipNeighbor(
        seed_content_id=seed_id,
        neighbor_content_id=neighbor_id,
        relationship_id=uuid4(),
        edge_score=score,
        algorithm_version="deterministic-v1",
    )


def test_aggregate_empty_inputs_and_seed_without_neighbors() -> None:
    """Empty graph inputs are valid and cannot create candidates."""

    aggregator = GraphCandidateAggregator()
    seed = uuid4()
    assert aggregator.aggregate([], [], 1) == []
    assert aggregator.aggregate([_seed(seed, 1)], [], 1) == []


def test_aggregate_uses_exact_decimal_score_and_rank_discount() -> None:
    """Contributions use the frozen edge_score / 100 / seed_rank formula."""

    first, second, target = uuid4(), uuid4(), uuid4()
    result = GraphCandidateAggregator().aggregate(
        [_seed(first, 1), _seed(second, 2)],
        [_neighbor(first, target, Decimal("25.50")), _neighbor(second, target, Decimal("50.00"))],
        10,
    )
    assert result == [
        GraphExpandedCandidate(
            content_id=target,
            graph_score=Decimal("0.505"),
            contributing_seed_ids=(first, second),
        )
    ]


def test_aggregate_deduplicates_logical_pairs_and_excludes_seed_targets() -> None:
    """Only the first pair contributes, while seed-to-seed edges are never candidates."""

    first, second, target = uuid4(), uuid4(), uuid4()
    first_neighbor = _neighbor(first, target, Decimal("80"))
    duplicate = RelationshipNeighbor(
        seed_content_id=first,
        neighbor_content_id=target,
        relationship_id=uuid4(),
        edge_score=Decimal("100"),
        algorithm_version="deterministic-v1",
    )
    result = GraphCandidateAggregator().aggregate(
        [_seed(first, 2), _seed(second, 1)],
        [first_neighbor, duplicate, _neighbor(second, target, Decimal("20")), _neighbor(first, second, Decimal("100"))],
        10,
    )
    assert result == [
        GraphExpandedCandidate(target, Decimal("0.6"), (second, first))
    ]


def test_aggregate_sorts_by_score_then_content_id_and_applies_limit_last() -> None:
    """Limit truncates the fully aggregated deterministic ranking only at the end."""

    seed = uuid4()
    low_id, high_id, combined_id = sorted((uuid4(), uuid4(), uuid4()), key=str)
    second = uuid4()
    result = GraphCandidateAggregator().aggregate(
        [_seed(seed, 1), _seed(second, 2)],
        [
            _neighbor(seed, low_id, Decimal("50")),
            _neighbor(seed, high_id, Decimal("50")),
            _neighbor(seed, combined_id, Decimal("30")),
            _neighbor(second, combined_id, Decimal("60")),
        ],
        2,
    )
    assert [(item.content_id, item.graph_score) for item in result] == [
        (combined_id, Decimal("0.6")),
        (low_id, Decimal("0.5")),
    ]


def test_aggregate_is_deterministic_without_mutating_inputs() -> None:
    """Neighbor order does not alter aggregation for distinct logical pairs."""

    first, second, target_a, target_b = uuid4(), uuid4(), uuid4(), uuid4()
    seeds = [_seed(first, 1), _seed(second, 2)]
    neighbors = [_neighbor(second, target_b, Decimal("40")), _neighbor(first, target_a, Decimal("40"))]
    original_seeds, original_neighbors = list(seeds), list(neighbors)
    aggregator = GraphCandidateAggregator()
    assert aggregator.aggregate(seeds, neighbors, 10) == aggregator.aggregate(seeds, list(reversed(neighbors)), 10)
    assert seeds == original_seeds and neighbors == original_neighbors


@pytest.mark.parametrize(
    ("seeds", "neighbors", "candidate_limit"),
    [
        (None, [], 1),
        ([], None, 1),
        (42, [], 1),
        ([object()], [], 1),
        ([GraphSeed("not-a-uuid", 1)], [], 1),
        ([_seed(uuid4(), 1), _seed(uuid4(), 1)], [], 1),
        ([_seed(UUID("00000000-0000-0000-0000-000000000001"), 1), _seed(UUID("00000000-0000-0000-0000-000000000001"), 2)], [], 1),
        ([_seed(uuid4(), 0)], [], 1),
        ([_seed(uuid4(), True)], [], 1),
        ([], [], 0),
        ([], [], True),
        ([], [], 1.0),
    ],
)
def test_aggregate_rejects_invalid_collections_seeds_and_limit(seeds: object, neighbors: object, candidate_limit: object) -> None:
    """All malformed top-level inputs fail with ValueError before aggregation."""

    with pytest.raises(ValueError):
        GraphCandidateAggregator().aggregate(seeds, neighbors, candidate_limit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "neighbor_factory",
    [
        lambda seed: _neighbor(uuid4(), uuid4()),
        lambda seed: _neighbor(seed, seed),
        lambda seed: _neighbor(seed, uuid4(), Decimal("-0.01")),
        lambda seed: _neighbor(seed, uuid4(), Decimal("100.01")),
        lambda seed: RelationshipNeighbor(seed, uuid4(), uuid4(), 50, "deterministic-v1"),
        lambda seed: object(),
    ],
)
def test_aggregate_rejects_invalid_neighbors_before_calculation(neighbor_factory) -> None:
    """Unknown seeds, self-loops and invalid Decimal scores cannot yield partial output."""

    seed = uuid4()
    valid = _neighbor(seed, uuid4(), Decimal("50"))
    with pytest.raises(ValueError):
        GraphCandidateAggregator().aggregate([_seed(seed, 1)], [valid, neighbor_factory(seed)], 10)

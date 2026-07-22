"""Unit tests for GraphExpansionService orchestration boundaries."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.repositories.content_relationship_repository import RelationshipNeighbor
from app.services.graph_candidate_aggregator import GraphExpandedCandidate, GraphSeed
from app.services.graph_expansion_service import GraphExpansionService


class SpyRelationshipRepository:
    """Record requested seed IDs without accessing a database."""

    def __init__(self, neighbors: list[RelationshipNeighbor] | None = None, error: Exception | None = None) -> None:
        self.calls: list[list[UUID]] = []
        self._neighbors = [] if neighbors is None else neighbors
        self._error = error

    def neighbors(self, content_ids: list[UUID]) -> list[RelationshipNeighbor]:
        self.calls.append(list(content_ids))
        if self._error is not None:
            raise self._error
        return self._neighbors


class SpyCandidateAggregator:
    """Record aggregation inputs and return a configured object unchanged."""

    def __init__(self, result: list[GraphExpandedCandidate] | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[GraphSeed], list[RelationshipNeighbor], int]] = []
        self._result = [] if result is None else result
        self._error = error

    def aggregate(
        self,
        *,
        seeds: list[GraphSeed],
        neighbors: list[RelationshipNeighbor],
        candidate_limit: int,
    ) -> list[GraphExpandedCandidate]:
        self.calls.append((list(seeds), list(neighbors), candidate_limit))
        if self._error is not None:
            raise self._error
        return self._result


def _candidate(content_id: UUID) -> GraphExpandedCandidate:
    """Create an immutable candidate used to check transparent return behavior."""

    return GraphExpandedCandidate(content_id, Decimal("0.5"), (uuid4(),))


def test_expand_empty_input_skips_all_dependencies() -> None:
    """An empty seed list is a no-op rather than a repository lookup."""

    repository, aggregator = SpyRelationshipRepository(), SpyCandidateAggregator()
    assert GraphExpansionService(repository, aggregator).expand([]) == []  # type: ignore[arg-type]
    assert repository.calls == [] and aggregator.calls == []


def test_expand_preserves_input_order_builds_ranks_and_returns_aggregator_result() -> None:
    """Input order is the sole source of graph seed rank and is never reordered."""

    first, second, third = uuid4(), uuid4(), uuid4()
    relationship_neighbor = RelationshipNeighbor(first, uuid4(), uuid4(), Decimal("50"), "deterministic-v1")
    expected = [_candidate(uuid4())]
    repository = SpyRelationshipRepository([relationship_neighbor])
    aggregator = SpyCandidateAggregator(expected)
    service = GraphExpansionService(repository, aggregator, candidate_limit=7)

    result = service.expand([third, first, second])

    assert result is expected
    assert repository.calls == [[third, first, second]]
    assert aggregator.calls == [
        ([GraphSeed(third, 1), GraphSeed(first, 2), GraphSeed(second, 3)], [relationship_neighbor], 7)
    ]


def test_expand_limits_seeds_before_repository_and_never_applies_a_second_cut() -> None:
    """Only the first max_seeds IDs reach the repository and aggregator."""

    ids = [uuid4() for _ in range(4)]
    expected = [_candidate(uuid4()), _candidate(uuid4())]
    repository, aggregator = SpyRelationshipRepository(), SpyCandidateAggregator(expected)
    result = GraphExpansionService(repository, aggregator, max_seeds=2, candidate_limit=1).expand(ids)

    assert result is expected
    assert repository.calls == [ids[:2]]
    assert aggregator.calls[0][0] == [GraphSeed(ids[0], 1), GraphSeed(ids[1], 2)]
    assert aggregator.calls[0][2] == 1


@pytest.mark.parametrize(
    "content_ids",
    [None, 42, "not-a-collection", b"not-a-collection", ["not-a-uuid"], [UUID("00000000-0000-0000-0000-000000000001")] * 2],
)
def test_expand_rejects_invalid_input_before_dependencies(content_ids: object) -> None:
    """Malformed input cannot yield a partial repository or aggregation call."""

    repository, aggregator = SpyRelationshipRepository(), SpyCandidateAggregator()
    with pytest.raises(ValueError):
        GraphExpansionService(repository, aggregator).expand(content_ids)  # type: ignore[arg-type]
    assert repository.calls == [] and aggregator.calls == []


@pytest.mark.parametrize(
    ("max_seeds", "candidate_limit"),
    [(0, 1), (True, 1), (1.0, 1), (1, 0), (1, True), (1, 1.0)],
)
def test_service_rejects_invalid_strict_limits(max_seeds: object, candidate_limit: object) -> None:
    """Constructor limits are strict positive integers and reject bool values."""

    with pytest.raises(ValueError):
        GraphExpansionService(SpyRelationshipRepository(), SpyCandidateAggregator(), max_seeds=max_seeds, candidate_limit=candidate_limit)  # type: ignore[arg-type]


def test_expand_propagates_repository_failure_without_calling_aggregator() -> None:
    """Repository errors fail closed and cannot produce a partial graph result."""

    repository = SpyRelationshipRepository(error=RuntimeError("repository unavailable"))
    aggregator = SpyCandidateAggregator()
    with pytest.raises(RuntimeError, match="repository unavailable"):
        GraphExpansionService(repository, aggregator).expand([uuid4()])
    assert len(repository.calls) == 1 and aggregator.calls == []


def test_expand_propagates_aggregator_failure_without_mutating_input() -> None:
    """Aggregator errors remain visible to the future boundary and input stays unchanged."""

    content_ids = [uuid4(), uuid4()]
    original = list(content_ids)
    repository = SpyRelationshipRepository()
    aggregator = SpyCandidateAggregator(error=RuntimeError("aggregation unavailable"))
    with pytest.raises(RuntimeError, match="aggregation unavailable"):
        GraphExpansionService(repository, aggregator).expand(content_ids)
    assert content_ids == original and len(repository.calls) == len(aggregator.calls) == 1


def test_expand_is_deterministic_for_repeated_calls() -> None:
    """The service introduces no mutable state or additional ordering behavior."""

    first, second = uuid4(), uuid4()
    expected = [_candidate(uuid4())]
    repository, aggregator = SpyRelationshipRepository(), SpyCandidateAggregator(expected)
    service = GraphExpansionService(repository, aggregator)
    assert service.expand([first, second]) is expected
    assert service.expand([first, second]) is expected
    assert repository.calls == [[first, second], [first, second]]

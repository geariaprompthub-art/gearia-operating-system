"""Pure deterministic aggregation for one-hop graph candidates."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.repositories.content_relationship_repository import RelationshipNeighbor


@dataclass(frozen=True)
class GraphSeed:
    """A ranked content identifier eligible to expand one logical graph hop."""

    content_id: UUID
    seed_rank: int


@dataclass(frozen=True)
class GraphExpandedCandidate:
    """An internally scored graph neighbor with its contributing seeds."""

    content_id: UUID
    graph_score: Decimal
    contributing_seed_ids: tuple[UUID, ...]


class GraphCandidateAggregator:
    """Aggregate logical neighbors without database, provider, or HTTP dependencies."""

    def aggregate(
        self,
        seeds: Sequence[GraphSeed],
        neighbors: Sequence[RelationshipNeighbor],
        candidate_limit: int,
    ) -> list[GraphExpandedCandidate]:
        """Aggregate valid one-hop contributions and apply the final global limit."""

        validated_seeds, validated_neighbors = self._validate_inputs(seeds, neighbors, candidate_limit)
        if not validated_seeds or not validated_neighbors:
            return []

        seed_ranks = {seed.content_id: seed.seed_rank for seed in validated_seeds}
        seed_ids = set(seed_ranks)
        seen_pairs: set[tuple[UUID, UUID]] = set()
        scores: dict[UUID, Decimal] = {}
        contributors: dict[UUID, set[UUID]] = {}

        for neighbor in validated_neighbors:
            pair = (neighbor.seed_content_id, neighbor.neighbor_content_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if neighbor.neighbor_content_id in seed_ids:
                continue

            contribution = (neighbor.edge_score / Decimal("100")) / Decimal(seed_ranks[neighbor.seed_content_id])
            scores[neighbor.neighbor_content_id] = scores.get(neighbor.neighbor_content_id, Decimal("0")) + contribution
            contributors.setdefault(neighbor.neighbor_content_id, set()).add(neighbor.seed_content_id)

        candidates = [
            GraphExpandedCandidate(
                content_id=content_id,
                graph_score=score,
                contributing_seed_ids=tuple(
                    sorted(contributors[content_id], key=lambda seed_id: (seed_ranks[seed_id], str(seed_id)))
                ),
            )
            for content_id, score in scores.items()
        ]
        return sorted(candidates, key=lambda item: (-item.graph_score, str(item.content_id)))[:candidate_limit]

    @classmethod
    def _validate_inputs(
        cls,
        seeds: Sequence[GraphSeed],
        neighbors: Sequence[RelationshipNeighbor],
        candidate_limit: int,
    ) -> tuple[list[GraphSeed], list[RelationshipNeighbor]]:
        """Validate the complete input set before any contribution is calculated."""

        if type(candidate_limit) is not int or candidate_limit < 1:
            raise ValueError("candidate_limit must be a positive integer")
        seed_values = cls._as_list(seeds, "seeds")
        neighbor_values = cls._as_list(neighbors, "neighbors")

        validated_seeds: list[GraphSeed] = []
        seed_ids: set[UUID] = set()
        seed_ranks: set[int] = set()
        for seed in seed_values:
            if not isinstance(seed, GraphSeed) or not isinstance(seed.content_id, UUID):
                raise ValueError("seeds must contain valid GraphSeed values")
            if type(seed.seed_rank) is not int or seed.seed_rank < 1:
                raise ValueError("seed_rank must be a positive integer")
            if seed.content_id in seed_ids:
                raise ValueError("seeds must not contain duplicate content_id values")
            if seed.seed_rank in seed_ranks:
                raise ValueError("seeds must not contain duplicate seed_rank values")
            seed_ids.add(seed.content_id)
            seed_ranks.add(seed.seed_rank)
            validated_seeds.append(seed)

        validated_neighbors: list[RelationshipNeighbor] = []
        for neighbor in neighbor_values:
            cls._validate_neighbor(neighbor, seed_ids)
            validated_neighbors.append(neighbor)
        return validated_seeds, validated_neighbors

    @staticmethod
    def _as_list(values: Sequence[object], name: str) -> list[object]:
        """Materialize an iterable while translating malformed collections to ValueError."""

        if values is None:
            raise ValueError(f"{name} must not be None")
        try:
            return list(values)
        except TypeError as error:
            raise ValueError(f"{name} must be iterable") from error

    @staticmethod
    def _validate_neighbor(neighbor: RelationshipNeighbor, seed_ids: set[UUID]) -> None:
        """Validate every relationship DTO without depending on its repository class."""

        required = ("seed_content_id", "neighbor_content_id", "relationship_id", "edge_score", "algorithm_version")
        if any(not hasattr(neighbor, attribute) for attribute in required):
            raise ValueError("neighbors must contain valid RelationshipNeighbor values")
        if not isinstance(neighbor.seed_content_id, UUID) or not isinstance(neighbor.neighbor_content_id, UUID):
            raise ValueError("neighbors must contain valid content UUID values")
        if not isinstance(neighbor.relationship_id, UUID):
            raise ValueError("neighbors must contain valid relationship UUID values")
        if neighbor.seed_content_id not in seed_ids:
            raise ValueError("neighbor references an unknown seed")
        if neighbor.seed_content_id == neighbor.neighbor_content_id:
            raise ValueError("neighbors must not contain self-loops")
        if not isinstance(neighbor.edge_score, Decimal) or not neighbor.edge_score.is_finite():
            raise ValueError("edge_score must be a finite Decimal")
        if neighbor.edge_score < Decimal("0") or neighbor.edge_score > Decimal("100"):
            raise ValueError("edge_score must be between 0 and 100")
        if not isinstance(neighbor.algorithm_version, str) or not neighbor.algorithm_version.strip():
            raise ValueError("algorithm_version must be a non-empty string")

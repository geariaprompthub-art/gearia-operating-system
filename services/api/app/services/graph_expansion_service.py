"""One-hop graph expansion orchestration with explicit internal dependencies."""

from collections.abc import Sequence
from uuid import UUID

from app.repositories.content_relationship_repository import ContentRelationshipRepository
from app.services.graph_candidate_aggregator import (
    GraphCandidateAggregator,
    GraphExpandedCandidate,
    GraphSeed,
)


class GraphExpansionService:
    """Build ranked graph seeds, then delegate reading and aggregation unchanged."""

    def __init__(
        self,
        relationship_repository: ContentRelationshipRepository,
        candidate_aggregator: GraphCandidateAggregator,
        *,
        max_seeds: int = 20,
        candidate_limit: int = 100,
    ) -> None:
        self._validate_limit(max_seeds, "max_seeds")
        self._validate_limit(candidate_limit, "candidate_limit")
        self._relationship_repository = relationship_repository
        self._candidate_aggregator = candidate_aggregator
        self._max_seeds = max_seeds
        self._candidate_limit = candidate_limit

    def expand(self, content_ids: Sequence[UUID]) -> list[GraphExpandedCandidate]:
        """Expand the first configured seeds through the injected graph components."""

        validated_content_ids = self._validate_content_ids(content_ids)
        if not validated_content_ids:
            return []

        selected_content_ids = validated_content_ids[: self._max_seeds]
        seeds = [
            GraphSeed(content_id=content_id, seed_rank=index)
            for index, content_id in enumerate(selected_content_ids, start=1)
        ]
        neighbors = self._relationship_repository.neighbors(selected_content_ids)
        return self._candidate_aggregator.aggregate(
            seeds=seeds,
            neighbors=neighbors,
            candidate_limit=self._candidate_limit,
        )

    @staticmethod
    def _validate_limit(value: int, name: str) -> None:
        """Require strict positive integer service limits at construction time."""

        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _validate_content_ids(content_ids: Sequence[UUID]) -> list[UUID]:
        """Reject all malformed input before either injected dependency is called."""

        if content_ids is None or isinstance(content_ids, (str, bytes)):
            raise ValueError("content_ids must be an iterable of UUID values")
        try:
            values = list(content_ids)
        except TypeError as error:
            raise ValueError("content_ids must be an iterable of UUID values") from error

        validated_ids: list[UUID] = []
        seen: set[UUID] = set()
        for content_id in values:
            if not isinstance(content_id, UUID):
                raise ValueError("content_ids must contain valid UUID values")
            if content_id in seen:
                raise ValueError("content_ids must not contain duplicates")
            seen.add(content_id)
            validated_ids.append(content_id)
        return validated_ids

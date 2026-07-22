"""Read-only projection of canonical content relationships into graph adjacency."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.content_relationship import ContentRelationship


@dataclass(frozen=True)
class RelationshipNeighbor:
    """One logical ``seed -> neighbor`` edge projected from canonical storage."""

    seed_content_id: UUID
    neighbor_content_id: UUID
    relationship_id: UUID
    edge_score: Decimal
    algorithm_version: str


class ContentRelationshipRepository:
    """Read canonical pairs once and expose only logical adjacency DTOs."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def neighbors(self, seed_content_ids: Sequence[UUID]) -> list[RelationshipNeighbor]:
        """Return deterministic logical neighbors for every validated seed.

        A pair can yield two DTOs when both endpoints were requested as seeds.  This
        is intentional: callers receive adjacency, not a deduplicated graph view.
        """

        validated_seed_ids = self._validate_seed_content_ids(seed_content_ids)
        if not validated_seed_ids:
            return []

        rows = self._database.execute(
            select(
                ContentRelationship.id,
                ContentRelationship.content_id,
                ContentRelationship.related_content_id,
                ContentRelationship.score,
                ContentRelationship.algorithm_version,
            ).where(
                or_(
                    ContentRelationship.content_id.in_(validated_seed_ids),
                    ContentRelationship.related_content_id.in_(validated_seed_ids),
                )
            )
        )
        seed_ids = set(validated_seed_ids)
        neighbors: list[RelationshipNeighbor] = []
        for relationship_id, content_id, related_content_id, score, algorithm_version in rows:
            if content_id == related_content_id:
                continue
            if content_id in seed_ids:
                neighbors.append(
                    RelationshipNeighbor(
                        seed_content_id=content_id,
                        neighbor_content_id=related_content_id,
                        relationship_id=relationship_id,
                        edge_score=score,
                        algorithm_version=algorithm_version,
                    )
                )
            if related_content_id in seed_ids:
                neighbors.append(
                    RelationshipNeighbor(
                        seed_content_id=related_content_id,
                        neighbor_content_id=content_id,
                        relationship_id=relationship_id,
                        edge_score=score,
                        algorithm_version=algorithm_version,
                    )
                )

        return sorted(
            neighbors,
            key=lambda item: (
                str(item.seed_content_id),
                -float(item.edge_score),
                str(item.neighbor_content_id),
                str(item.relationship_id),
            ),
        )

    @staticmethod
    def _validate_seed_content_ids(seed_content_ids: Sequence[UUID]) -> list[UUID]:
        """Reject invalid or duplicate seeds before issuing any database query."""

        if seed_content_ids is None:
            raise ValueError("seed_content_ids must not be None")
        try:
            values = list(seed_content_ids)
        except TypeError as error:
            raise ValueError("seed_content_ids must be a sequence of UUID values") from error

        validated_ids: list[UUID] = []
        seen: set[UUID] = set()
        for content_id in values:
            if not isinstance(content_id, UUID):
                raise ValueError("seed_content_ids must contain valid UUID values")
            if content_id in seen:
                raise ValueError("seed_content_ids must not contain duplicates")
            seen.add(content_id)
            validated_ids.append(content_id)
        return validated_ids

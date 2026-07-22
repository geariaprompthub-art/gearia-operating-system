"""Pure, deterministic Reciprocal Rank Fusion for internal retrieval candidates."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class ContentIdCandidate(Protocol):
    """Minimum candidate interface shared by lexical and vector retrieval layers."""

    content_id: UUID


@dataclass(frozen=True)
class RankedList:
    """One ordered retrieval result list and its source identifier."""

    origin: str
    candidates: Sequence[ContentIdCandidate]


@dataclass(frozen=True)
class FusedCandidate:
    """Internal fused result; the score is never a public API field."""

    content_id: UUID
    rrf_score: float
    matched_by: tuple[str, ...]


class ReciprocalRankFusion:
    """Fuse ordered candidate lists by position without database or provider dependencies."""

    DEFAULT_RRF_K = 60

    @classmethod
    def fuse(
        cls,
        ranked_lists: Sequence[RankedList],
        top_k: int,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> list[FusedCandidate]:
        """Combine lists using `sum(1 / (rrf_k + position))` and deterministic ties."""

        cls._validate_positive_integer("top_k", top_k)
        cls._validate_positive_integer("rrf_k", rrf_k)
        validated_lists = cls._validate_ranked_lists(ranked_lists)
        scores: dict[UUID, float] = {}
        origins: dict[UUID, set[str]] = {}
        for origin, ranked_list in validated_lists:
            seen_in_origin: set[UUID] = set()
            for position, candidate in enumerate(ranked_list.candidates, start=1):
                content_id = cls._validate_content_id(candidate)
                if content_id in seen_in_origin:
                    continue
                seen_in_origin.add(content_id)
                scores[content_id] = scores.get(content_id, 0.0) + 1.0 / (rrf_k + position)
                origins.setdefault(content_id, set()).add(origin)
        fused = [
            FusedCandidate(
                content_id=content_id,
                rrf_score=score,
                matched_by=cls._canonical_origins(origins[content_id]),
            )
            for content_id, score in scores.items()
        ]
        return sorted(fused, key=lambda candidate: (-candidate.rrf_score, candidate.content_id))[:top_k]

    @staticmethod
    def _validate_positive_integer(name: str, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be an integer greater than or equal to one")

    @staticmethod
    def _validate_origin(origin: str) -> str:
        if not isinstance(origin, str) or not origin:
            raise ValueError("origin must not be empty")
        if origin != origin.strip():
            raise ValueError("origin must be canonical without surrounding whitespace")
        return origin

    @classmethod
    def _validate_ranked_lists(cls, ranked_lists: Sequence[RankedList]) -> list[tuple[str, RankedList]]:
        """Validate every source before fusion so duplicate origins cannot partially contribute."""

        seen_origins: set[str] = set()
        validated: list[tuple[str, RankedList]] = []
        for ranked_list in ranked_lists:
            origin = cls._validate_origin(ranked_list.origin)
            if origin in seen_origins:
                raise ValueError(f"duplicate origin: {origin}")
            seen_origins.add(origin)
            validated.append((origin, ranked_list))
        return validated

    @staticmethod
    def _validate_content_id(candidate: ContentIdCandidate) -> UUID:
        content_id = getattr(candidate, "content_id", None)
        if not isinstance(content_id, UUID):
            raise ValueError("candidate must have a valid UUID content_id")
        return content_id

    @staticmethod
    def _canonical_origins(origins: set[str]) -> tuple[str, ...]:
        priority = [origin for origin in ("lexical", "vector") if origin in origins]
        remaining = sorted(origin for origin in origins if origin not in {"lexical", "vector"})
        return tuple(priority + remaining)

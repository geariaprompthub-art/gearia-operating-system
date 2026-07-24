"""Pure internal contracts for the provider-agnostic reranking stage."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """Prepared textual candidate received by the reranking boundary."""

    content_id: UUID
    document_text: str
    pre_rerank_rank: int
    matched_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderRerankResult:
    """Provider-private score associated with one supplied content identifier."""

    content_id: UUID
    score: float


@dataclass(frozen=True, slots=True)
class RerankedCandidate:
    """Validated provider ordering with internal score and immutable provenance."""

    content_id: UUID
    pre_rerank_rank: int
    matched_by: tuple[str, ...]
    rerank_score: float


class RerankingProvider(Protocol):
    """Provider adapter contract with no SDK, persistence, or HTTP coupling."""

    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> Sequence[ProviderRerankResult]:
        """Return one result per candidate, covering all IDs in provider-defined order."""

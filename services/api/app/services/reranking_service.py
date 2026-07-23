"""Pure validation and deterministic ordering for provider-agnostic reranking."""

from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from uuid import UUID

from app.services.reranking_contracts import (
    ProviderRerankResult,
    RerankCandidate,
    RerankedCandidate,
    RerankingProvider,
)


class RerankingService:
    """Validate complete reranking exchanges before returning any reordered result."""

    MAX_CANDIDATES = 100
    _MATCHED_BY_ORDER = ("lexical", "vector", "graph")
    _MATCHED_BY_VALUES = frozenset(_MATCHED_BY_ORDER)

    def __init__(self, provider: RerankingProvider) -> None:
        self._provider = provider

    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> list[RerankedCandidate]:
        """Score an unchanged candidate sequence once and return deterministic ordering."""

        normalized_query = self._validate_query(query)
        validated_candidates = self._validate_candidates(candidates)
        if not validated_candidates:
            return []

        provider_results = self._provider.rerank(normalized_query, validated_candidates)
        scores = self._validate_provider_results(provider_results, validated_candidates)
        return [
            RerankedCandidate(
                content_id=candidate.content_id,
                pre_rerank_rank=candidate.pre_rerank_rank,
                matched_by=candidate.matched_by,
            )
            for candidate in sorted(validated_candidates, key=lambda candidate: self._ordering_key(candidate, scores))
        ]

    @staticmethod
    def _ordering_key(candidate: RerankCandidate, scores: Mapping[UUID, float]) -> tuple[float, int, UUID]:
        """Keep the UUID tie-break explicit even though valid ranks are unique."""

        return (-scores[candidate.content_id], candidate.pre_rerank_rank, candidate.content_id)

    @staticmethod
    def _validate_query(query: str) -> str:
        if not isinstance(query, str):
            raise ValueError("query must be a non-blank string")
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must be a non-blank string")
        return normalized_query

    @classmethod
    def _validate_candidates(cls, candidates: Sequence[RerankCandidate]) -> list[RerankCandidate]:
        values = cls._materialize(candidates, "candidates")
        if len(values) > cls.MAX_CANDIDATES:
            raise ValueError(f"candidates must not exceed {cls.MAX_CANDIDATES}")

        seen_ids: set[UUID] = set()
        seen_ranks: set[int] = set()
        validated: list[RerankCandidate] = []
        for candidate in values:
            cls._validate_candidate(candidate)
            if candidate.content_id in seen_ids:
                raise ValueError("candidates must not contain duplicate content_id values")
            if candidate.pre_rerank_rank in seen_ranks:
                raise ValueError("candidates must not contain duplicate pre_rerank_rank values")
            seen_ids.add(candidate.content_id)
            seen_ranks.add(candidate.pre_rerank_rank)
            validated.append(candidate)
        return validated

    @classmethod
    def _validate_candidate(cls, candidate: object) -> None:
        if not isinstance(candidate, RerankCandidate):
            raise ValueError("candidates must contain RerankCandidate values")
        if not isinstance(candidate.content_id, UUID):
            raise ValueError("candidate content_id must be a UUID")
        if not isinstance(candidate.document_text, str) or not candidate.document_text.strip():
            raise ValueError("candidate document_text must be a non-blank string")
        if type(candidate.pre_rerank_rank) is not int or candidate.pre_rerank_rank < 1:
            raise ValueError("candidate pre_rerank_rank must be an integer greater than or equal to one")
        if not isinstance(candidate.matched_by, tuple) or not candidate.matched_by:
            raise ValueError("candidate matched_by must be a non-empty tuple")
        if any(type(origin) is not str or origin not in cls._MATCHED_BY_VALUES for origin in candidate.matched_by):
            raise ValueError("candidate matched_by contains an unsupported origin")
        if len(set(candidate.matched_by)) != len(candidate.matched_by):
            raise ValueError("candidate matched_by must not contain duplicates")
        canonical = tuple(origin for origin in cls._MATCHED_BY_ORDER if origin in candidate.matched_by)
        if candidate.matched_by != canonical:
            raise ValueError("candidate matched_by must use canonical origin order")

    @classmethod
    def _validate_provider_results(
        cls,
        provider_results: Sequence[ProviderRerankResult],
        candidates: Sequence[RerankCandidate],
    ) -> dict[UUID, float]:
        values = cls._materialize(provider_results, "provider results")
        if len(values) != len(candidates):
            raise ValueError("provider results must contain exactly one result per candidate")

        expected_ids = {candidate.content_id for candidate in candidates}
        scores: dict[UUID, float] = {}
        for result in values:
            if not isinstance(result, ProviderRerankResult):
                raise ValueError("provider results must contain ProviderRerankResult values")
            if not isinstance(result.content_id, UUID):
                raise ValueError("provider result content_id must be a UUID")
            if result.content_id not in expected_ids:
                raise ValueError("provider result references an unknown content_id")
            if result.content_id in scores:
                raise ValueError("provider results must not contain duplicate content_id values")
            if type(result.score) not in (int, float):
                raise ValueError("provider result score must be an integer or float")
            score = float(result.score)
            if not isfinite(score):
                raise ValueError("provider result score must be finite")
            scores[result.content_id] = score

        if set(scores) != expected_ids:
            raise ValueError("provider results must include every candidate content_id")
        return scores

    @staticmethod
    def _materialize(values: object, name: str) -> list[object]:
        if values is None or isinstance(values, (str, bytes)):
            raise ValueError(f"{name} must be an iterable")
        if not isinstance(values, Iterable):
            raise ValueError(f"{name} must be an iterable")
        try:
            return list(values)
        except TypeError as error:
            raise ValueError(f"{name} must be an iterable") from error

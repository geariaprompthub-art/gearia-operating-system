"""Pure validation and deterministic ordering for provider-agnostic reranking."""

from collections.abc import Iterable, Sequence
from math import isfinite
from uuid import UUID

from app.services.reranking_contracts import (
    ProviderRerankResult,
    RerankCandidate,
    RerankedCandidate,
    RerankingProvider,
)
from app.services.reranking_provider_errors import RerankingProviderResponseError


class RerankingService:
    """Validate complete reranking exchanges while preserving provider order."""

    MAX_CANDIDATES = 100
    _MATCHED_BY_ORDER = ("lexical", "vector", "graph")
    _MATCHED_BY_VALUES = frozenset(_MATCHED_BY_ORDER)

    def __init__(self, provider: RerankingProvider) -> None:
        self._provider = provider

    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> list[RerankedCandidate]:
        """Score an unchanged candidate sequence once and return deterministic ordering."""

        validated_query = self._validate_query(query)
        validated_candidates = self._validate_candidates(candidates)
        if not validated_candidates:
            return []

        provider_results = self._provider.rerank(validated_query, validated_candidates)
        validated_results = self._validate_provider_results(provider_results, validated_candidates)
        candidates_by_id = {candidate.content_id: candidate for candidate in validated_candidates}
        return [
            RerankedCandidate(
                content_id=result.content_id,
                rerank_score=result.score,
                pre_rerank_rank=candidates_by_id[result.content_id].pre_rerank_rank,
                matched_by=candidates_by_id[result.content_id].matched_by,
            )
            for result in validated_results
        ]

    @staticmethod
    def _validate_query(query: str) -> str:
        if not isinstance(query, str):
            raise ValueError("query must be a non-blank string")
        if not query.strip():
            raise ValueError("query must be a non-blank string")
        return query

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
    ) -> list[ProviderRerankResult]:
        try:
            values = cls._materialize(provider_results, "provider results")
        except ValueError as error:
            raise RerankingProviderResponseError(
                "Reranking provider returned an invalid response"
            ) from error
        if len(values) != len(candidates):
            raise RerankingProviderResponseError(
                "Reranking provider returned an unexpected result count"
            )

        expected_ids = {candidate.content_id for candidate in candidates}
        seen_ids: set[UUID] = set()
        validated: list[ProviderRerankResult] = []
        for result in values:
            if not isinstance(result, ProviderRerankResult):
                raise RerankingProviderResponseError(
                    "Reranking provider returned an invalid response"
                )
            if not isinstance(result.content_id, UUID):
                raise RerankingProviderResponseError(
                    "Reranking provider returned an invalid content identifier"
                )
            if result.content_id not in expected_ids:
                raise RerankingProviderResponseError(
                    "Reranking provider returned an unknown content identifier"
                )
            if result.content_id in seen_ids:
                raise RerankingProviderResponseError(
                    "Reranking provider returned duplicate content identifiers"
                )
            if type(result.score) not in (int, float):
                raise RerankingProviderResponseError(
                    "Reranking provider returned an invalid score"
                )
            score = float(result.score)
            if not isfinite(score):
                raise RerankingProviderResponseError(
                    "Reranking provider returned an invalid score"
                )
            seen_ids.add(result.content_id)
            validated.append(ProviderRerankResult(result.content_id, score))

        if seen_ids != expected_ids:
            raise RerankingProviderResponseError(
                "Reranking provider returned an unexpected result set"
            )
        return validated

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

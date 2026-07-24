"""Voyage AI adapter isolated behind the internal reranking provider protocol."""

from collections.abc import Sequence
from math import isfinite
from typing import Protocol

import voyageai
from voyageai.error import (
    APIConnectionError,
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from app.services.reranking_contracts import ProviderRerankResult, RerankCandidate
from app.services.reranking_provider_errors import (
    RerankingProviderConfigurationError,
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)


class VoyageClient(Protocol):
    """Small injectable boundary over the only SDK operation used by this adapter."""

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str,
        top_k: None,
        truncation: bool,
    ) -> object:
        """Return the provider-private reranking response."""


class VoyageRerankingProvider:
    """Map Voyage index-based results to internal candidates without retries or fallback.

    ``max_retries=0`` disables logical client retries. Transport retries internal
    to the official SDK are outside its supported public configuration surface.
    """

    def __init__(
        self,
        api_key: str | None,
        model: str,
        timeout_seconds: float,
        client: VoyageClient | None = None,
    ) -> None:
        normalized_timeout = self._validate_configuration(
            api_key,
            model,
            timeout_seconds,
            client,
        )
        self._model = model
        self._client: VoyageClient = (
            client
            if client is not None
            else voyageai.Client(
                api_key=api_key,
                max_retries=0,
                timeout=normalized_timeout,
            )
        )

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> list[ProviderRerankResult]:
        """Call Voyage once and preserve its returned order through index mapping."""

        values = list(candidates)
        if not values:
            return []
        documents = [candidate.document_text for candidate in values]
        try:
            response = self._client.rerank(
                query=query,
                documents=documents,
                model=self._model,
                top_k=None,
                truncation=True,
            )
        except (AuthenticationError, InvalidRequestError) as error:
            raise RerankingProviderConfigurationError(
                "Voyage reranking provider configuration is invalid"
            ) from error
        except (
            Timeout,
            APIConnectionError,
            RateLimitError,
            ServiceUnavailableError,
        ) as error:
            raise RerankingProviderUnavailableError(
                "Voyage reranking provider unavailable"
            ) from error
        return self._map_response(response, values)

    @staticmethod
    def _validate_configuration(
        api_key: str | None,
        model: str,
        timeout_seconds: float,
        client: VoyageClient | None,
    ) -> float:
        if client is None and (not isinstance(api_key, str) or not api_key.strip()):
            raise RerankingProviderConfigurationError(
                "Voyage reranking provider is not configured"
            )
        if not isinstance(model, str) or not model.strip():
            raise RerankingProviderConfigurationError(
                "Voyage reranking model is not configured"
            )
        if (
            type(timeout_seconds) not in (int, float)
            or isinstance(timeout_seconds, bool)
            or not isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise RerankingProviderConfigurationError(
                "Voyage reranking timeout is invalid"
            )
        return float(timeout_seconds)

    @classmethod
    def _map_response(
        cls, response: object, candidates: Sequence[RerankCandidate]
    ) -> list[ProviderRerankResult]:
        results = getattr(response, "results", None)
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            raise RerankingProviderResponseError(
                "Voyage reranking provider returned an invalid response"
            )
        if len(results) != len(candidates):
            raise RerankingProviderResponseError(
                "Voyage reranking provider returned an incomplete response"
            )

        mapped: list[ProviderRerankResult] = []
        seen_indices: set[int] = set()
        for result in results:
            index = getattr(result, "index", None)
            score = getattr(result, "relevance_score", None)
            if (
                type(index) is not int
                or index < 0
                or index >= len(candidates)
                or index in seen_indices
            ):
                raise RerankingProviderResponseError(
                    "Voyage reranking provider returned invalid result indices"
                )
            if (
                type(score) not in (int, float)
                or isinstance(score, bool)
                or not isfinite(float(score))
            ):
                raise RerankingProviderResponseError(
                    "Voyage reranking provider returned invalid result scores"
                )
            seen_indices.add(index)
            mapped.append(ProviderRerankResult(content_id=candidates[index].content_id, score=float(score)))

        if len(seen_indices) != len(candidates):
            raise RerankingProviderResponseError(
                "Voyage reranking provider returned an incomplete response"
            )
        return mapped

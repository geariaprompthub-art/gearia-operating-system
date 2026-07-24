"""Sanitized infrastructure errors exposed by reranking provider adapters."""


class RerankingProviderError(RuntimeError):
    """Base error for provider failures that must not reveal vendor details."""


class RerankingProviderConfigurationError(RerankingProviderError):
    """Raised when a production provider cannot be constructed safely."""


class RerankingProviderUnavailableError(RerankingProviderError):
    """Raised when the external provider call cannot complete."""


class RerankingProviderResponseError(RerankingProviderError):
    """Raised when a provider response cannot satisfy the internal contract."""

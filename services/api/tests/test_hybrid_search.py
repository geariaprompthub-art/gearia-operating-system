"""HTTP translation coverage for Sprint 12 hybrid reranking integration."""

from uuid import uuid4

import pytest

from app.core.config import Settings
from app.services.hybrid_reranking_pipeline import RerankingPipelineHydrationError
from app.services.reranking_provider_errors import (
    RerankingProviderConfigurationError,
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)
from app.services.hybrid_search_dependencies import get_hybrid_search_service
from test_main import client


def _assert_public_safe(value: object) -> None:
    """Reject private provider, vector, and internal ranking fields recursively."""

    forbidden = {
        "embedding",
        "vector",
        "embedding_vector",
        "values",
        "rrf_score",
        "graph_score",
        "edge_score",
        "similarity",
        "distance",
        "provider",
        "model",
        "dimensions",
        "strategy",
    }
    if isinstance(value, dict):
        assert not forbidden & set(value)
        for nested in value.values():
            _assert_public_safe(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_public_safe(nested)


class Service:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, top_k: int) -> dict[str, object]:
        self.calls.append((query, top_k))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value  # type: ignore[return-value]


def test_hybrid_endpoint_preserves_pipeline_response() -> None:
    from app.main import app

    result = {"items": [{"rank": 1, "content_id": str(uuid4()), "title": "A", "url": "https://x", "summary": None, "matched_by": ["graph"]}], "total": 1}
    service = Service(result)
    app.dependency_overrides[get_hybrid_search_service] = lambda: service
    try:
        response = client.post("/search/hybrid", json={"query": " query ", "top_k": 1})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)
    assert response.status_code == 200
    assert response.json() == result
    assert service.calls == [("query", 1)]
    _assert_public_safe(response.json())


def test_hybrid_http_contract_validation_errors_and_safe_payloads() -> None:
    """The original public request contract remains strict after reranking integration."""

    from app.main import app

    result = {
        "items": [
            {
                "rank": 1,
                "content_id": str(uuid4()),
                "title": "Hybrid",
                "url": "https://test/hybrid",
                "summary": None,
                "matched_by": ["lexical", "vector"],
            }
        ],
        "total": 1,
    }
    service = Service(result)
    app.dependency_overrides[get_hybrid_search_service] = lambda: service
    try:
        valid = client.post("/search/hybrid", json={"query": "  query  "})
        maximum = client.post("/search/hybrid", json={"query": "x" * 8000, "top_k": 100})
        invalid = [
            client.post("/search/hybrid", json={"query": "   "}),
            client.post("/search/hybrid", json={"query": "x" * 8001}),
            client.post("/search/hybrid", json={"query": "query", "top_k": 0}),
            client.post("/search/hybrid", json={"query": "query", "top_k": 101}),
            client.post("/search/hybrid", json={"query": "query", "rrf_k": 60}),
            client.post("/search/hybrid", json={"query": "query", "top_k": True}),
            client.post("/search/hybrid", json={"query": 42}),
        ]
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)

    assert valid.status_code == maximum.status_code == 200
    assert service.calls == [("query", 20), ("x" * 8000, 100)]
    assert all(response.status_code == 422 for response in invalid)
    for response in [valid, maximum, *invalid]:
        _assert_public_safe(response.json())


def test_hybrid_endpoint_reports_missing_voyage_configuration_without_breaking_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dependency resolution errors use the global boundary without blocking startup."""

    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.get_settings",
        lambda: Settings(voyage_api_key=None),
    )
    response = client.post("/search/hybrid", json={"query": "query"})
    health = client.get("/health")

    assert response.status_code == 500
    assert response.json() == {"detail": "Reranking service is not configured"}
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert all(term not in response.text for term in ("VOYAGE_API_KEY", "Voyage", "api_key", "rerank-2.5"))


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (RerankingProviderConfigurationError("secret"), 500, "Reranking service is not configured"),
        (RerankingProviderUnavailableError("secret"), 503, "Reranking service is temporarily unavailable"),
        (RerankingProviderResponseError("secret"), 502, "Reranking service returned an invalid response"),
        (RerankingPipelineHydrationError("secret"), 500, "Search result hydration failed"),
    ],
)
def test_hybrid_endpoint_sanitizes_reranking_errors(error: Exception, status_code: int, detail: str) -> None:
    from app.main import app

    app.dependency_overrides[get_hybrid_search_service] = lambda: Service(error)
    try:
        response = client.post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)
    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    _assert_public_safe(response.json())


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (RuntimeError("upstream secret"), 503, "Hybrid retrieval unavailable"),
        (ValueError("internal secret"), 500, "Hybrid retrieval failed"),
    ],
)
def test_hybrid_endpoint_preserves_generic_failure_contract(
    error: Exception, status_code: int, detail: str
) -> None:
    """Unrelated failure handling remains unchanged while reranking uses global handlers."""

    from app.main import app

    app.dependency_overrides[get_hybrid_search_service] = lambda: Service(error)
    try:
        response = client.post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    _assert_public_safe(response.json())

"""HTTP translation coverage for Sprint 12 hybrid reranking integration."""

from uuid import uuid4

import pytest
from decimal import Decimal
from prometheus_client import CollectorRegistry

from app.core.config import Settings
from app.services.hybrid_reranking_pipeline import RerankingPipelineHydrationError
from app.services.reranking_provider_errors import (
    RerankingProviderConfigurationError,
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)
from app.services.hybrid_search_dependencies import get_hybrid_search_service
from app.services.hybrid_search_dependencies import get_hybrid_search_telemetry
from app.core.prometheus_hybrid_search_telemetry import PrometheusHybridSearchTelemetry
from app.services.hybrid_search_telemetry import NoOpHybridSearchTelemetry
from app.repositories.lexical_search_repository import LexicalSearchCandidate
from app.repositories.vector_search_repository import VectorSearchCandidate
from app.repositories.rerank_document_repository import RerankDocumentRecord
from app.repositories.content_hydration_repository import HydratedContent
from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.reranking_contracts import ProviderRerankResult
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


class FailingTelemetry:
    """Strict HTTP-level probe: every observation path raises."""
    def record_request_started(self) -> None: raise RuntimeError("telemetry")
    def record_request_completed(self, *args: object, **kwargs: object) -> None: raise RuntimeError("telemetry")
    def record_stage_completed(self, *args: object, **kwargs: object) -> None: raise RuntimeError("telemetry")
    def record_stage_failed(self, *args: object, **kwargs: object) -> None: raise RuntimeError("telemetry")
    def record_provider_call(self, *args: object, **kwargs: object) -> None: raise RuntimeError("telemetry")


def _install_real_hybrid_path_fakes(monkeypatch: pytest.MonkeyPatch, provider_error: Exception | None = None, invalid_provider_response: bool = False, hydration_error: bool = False) -> list[list[object]]:
    """Keep FastAPI DI and the real service/pipeline while faking only IO boundaries."""
    from app.services import hybrid_search_dependencies as dependencies

    first, second = uuid4(), uuid4()
    calls: list[list[object]] = []
    class Lexical:
        def __init__(self, _: object) -> None: pass
        def search(self, _: str, __: int) -> list[LexicalSearchCandidate]: return [LexicalSearchCandidate(first)]
    class Vector:
        def __init__(self, *_: object) -> None: pass
        def search(self, _: str, __: int, ___: float) -> list[VectorSearchCandidate]: return [VectorSearchCandidate(second, 0.9)]
    class VectorRepository:
        def __init__(self, _: object) -> None: pass
    class Relationships:
        def __init__(self, _: object) -> None: pass
    class Graph:
        def __init__(self, *_: object, **__: object) -> None: pass
        def expand(self, _: list[object]) -> list[GraphExpandedCandidate]: return []
    class Eligible:
        def __init__(self, _: object) -> None: pass
        def filter_eligible(self, ids: list[object]) -> list[object]: return list(ids)
    class Documents:
        def __init__(self, _: object) -> None: pass
        def hydrate(self, ids: list[object]) -> list[RerankDocumentRecord]: return [RerankDocumentRecord(content_id, "Title", "Summary", None, (), ()) for content_id in ids]
    class Hydration:
        def __init__(self, _: object) -> None: pass
        def hydrate(self, ids: list[object]) -> list[HydratedContent]:
            if hydration_error: return []
            return [HydratedContent(content_id, "Title", "https://test", "Summary") for content_id in ids]
    class Provider:
        def __init__(self, **_: object) -> None: pass
        def rerank(self, _: str, candidates: list[object]) -> list[ProviderRerankResult]:
            calls.append(list(candidates))
            if provider_error: raise provider_error
            if invalid_provider_response: return [ProviderRerankResult(uuid4(), 1.0)]
            return [ProviderRerankResult(candidate.content_id, float(len(candidates) - index)) for index, candidate in enumerate(candidates)]
    monkeypatch.setattr(dependencies, "LexicalSearchRepository", Lexical)
    monkeypatch.setattr(dependencies, "VectorSearchRepository", VectorRepository)
    monkeypatch.setattr(dependencies, "VectorCandidateService", Vector)
    monkeypatch.setattr(dependencies, "ContentRelationshipRepository", Relationships)
    monkeypatch.setattr(dependencies, "GraphExpansionService", Graph)
    monkeypatch.setattr(dependencies, "ContentEligibilityRepository", Eligible)
    monkeypatch.setattr(dependencies, "RerankDocumentRepository", Documents)
    monkeypatch.setattr(dependencies, "ContentHydrationRepository", Hydration)
    monkeypatch.setattr(dependencies, "VoyageRerankingProvider", Provider)
    return calls


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


def test_hybrid_http_fail_open_preserves_real_service_pipeline_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only telemetry is overridden; FastAPI resolves the actual composition root."""
    from app.main import app
    calls = _install_real_hybrid_path_fakes(monkeypatch)
    responses = []
    try:
        app.dependency_overrides[get_hybrid_search_telemetry] = lambda: NoOpHybridSearchTelemetry()
        responses.append(client.post("/search/hybrid", json={"query": "query", "top_k": 2}))
        app.dependency_overrides[get_hybrid_search_telemetry] = lambda: FailingTelemetry()
        responses.append(client.post("/search/hybrid", json={"query": "query", "top_k": 2}))
    finally:
        app.dependency_overrides.pop(get_hybrid_search_telemetry, None)
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert len(calls) == 2
    assert all(len(call) == 2 for call in calls)
    assert all("telemetry" not in response.text.lower() for response in responses)
    _assert_public_safe(responses[0].json())


def test_hybrid_http_fail_open_preserves_provider_unavailable_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed observation cannot replace the provider error translated by FastAPI."""
    from app.main import app
    calls = _install_real_hybrid_path_fakes(monkeypatch, RerankingProviderUnavailableError("secret"))
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: FailingTelemetry()
    try:
        response = client.post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_telemetry, None)
    assert response.status_code == 503
    assert response.json() == {"detail": "Reranking service is temporarily unavailable"}
    assert len(calls) == 1
    assert "telemetry" not in response.text.lower()


def _metric_value(registry: CollectorRegistry, name: str, **labels: str) -> float:
    return sum(
        sample.value for family in registry.collect() for sample in family.samples
        if sample.name == name and sample.labels == labels
    )


@pytest.mark.parametrize(
    ("provider_error", "invalid_response", "hydration_error", "status_code", "detail", "provider_calls", "failed_stage", "provider_error_calls"),
    [
        (RerankingProviderUnavailableError("secret"), False, False, 503, "Reranking service is temporarily unavailable", 1, "provider_reranking", 1),
        (None, True, False, 502, "Reranking service returned an invalid response", 1, "provider_reranking", 1),
        (None, False, True, 500, "Search result hydration failed", 1, "public_hydration", 0),
    ],
)
def test_hybrid_http_error_contracts_with_isolated_prometheus(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception | None,
    invalid_response: bool,
    hydration_error: bool,
    status_code: int,
    detail: str,
    provider_calls: int,
    failed_stage: str,
    provider_error_calls: int,
) -> None:
    """Real DI preserves handlers while exposing only error observations internally."""
    from app.main import app
    registry = CollectorRegistry()
    telemetry = PrometheusHybridSearchTelemetry(registry=registry)
    calls = _install_real_hybrid_path_fakes(monkeypatch, provider_error, invalid_response, hydration_error)
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: telemetry
    try:
        response = client.post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_telemetry, None)
    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert len(calls) == provider_calls
    assert "prometheus" not in response.text.lower()
    assert _metric_value(registry, "hybrid_search_requests_total", status="error") == 1
    assert _metric_value(registry, "hybrid_search_provider_calls_total", status="error") == provider_error_calls
    assert _metric_value(registry, "hybrid_search_stage_duration_seconds_count", stage=failed_stage, status="error") == 1


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


def test_hybrid_configuration_error_preserves_http_contract_with_isolated_prometheus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration fails before service execution, so it creates no false observations."""
    from app.main import app
    registry = CollectorRegistry()
    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.get_settings",
        lambda: Settings(voyage_api_key=None),
    )
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: PrometheusHybridSearchTelemetry(registry=registry)
    try:
        response = client.post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_telemetry, None)
    assert response.status_code == 500
    assert response.json() == {"detail": "Reranking service is not configured"}
    assert _metric_value(registry, "hybrid_search_requests_total", status="error") == 0
    assert _metric_value(registry, "hybrid_search_provider_calls_total", status="error") == 0


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

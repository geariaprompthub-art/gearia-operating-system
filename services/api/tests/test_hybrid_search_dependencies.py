"""Composition-root coverage for Sprint 12 hybrid reranking."""

import pytest

from app.core.config import Settings
from app.services.fake_embedding_provider import FakeEmbeddingProvider
from app.services.hybrid_search_dependencies import (
    get_hybrid_search_service,
    get_hybrid_search_telemetry,
)
from app.services.hybrid_search_telemetry import NoOpHybridSearchTelemetry
from app.services.reranking_provider_errors import RerankingProviderConfigurationError
from app.services.voyage_reranking_provider import VoyageRerankingProvider
from test_main import TestingSessionLocal


class Provider:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def rerank(self, query: str, candidates: object) -> list[object]:
        return []


def test_hybrid_factory_builds_pipeline_with_one_shared_session(monkeypatch) -> None:
    database = TestingSessionLocal()
    settings = Settings(voyage_api_key="test-key")
    monkeypatch.setattr("app.services.hybrid_search_dependencies.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.hybrid_search_dependencies.VoyageRerankingProvider", Provider)
    try:
        service = get_hybrid_search_service(database=database, provider=FakeEmbeddingProvider())
        pipeline = service._reranking_pipeline
        assert isinstance(pipeline._reranker._provider, Provider)
        assert pipeline._reranker._provider.kwargs == {
            "api_key": "test-key", "model": "rerank-2.5", "timeout_seconds": 10.0
        }
        assert pipeline._eligibility._database is database
        assert pipeline._documents._database is database
        assert pipeline._hydration._database is database
    finally:
        database.close()


def test_hybrid_factory_propagates_missing_reranking_configuration(monkeypatch) -> None:
    """The composition root has no HTTP translation and performs no provider request."""

    database = TestingSessionLocal()
    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.get_settings",
        lambda: Settings(voyage_api_key=None),
    )
    try:
        with pytest.raises(RerankingProviderConfigurationError, match="not configured"):
            get_hybrid_search_service(database=database, provider=FakeEmbeddingProvider())
    finally:
        database.close()


def test_telemetry_dependency_selects_noop_without_creating_a_prometheus_adapter(monkeypatch) -> None:
    """Disabled observation remains a pure composition-root choice."""

    get_hybrid_search_telemetry.cache_clear()
    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.get_settings",
        lambda: Settings(hybrid_search_telemetry_enabled=False),
    )
    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.PrometheusHybridSearchTelemetry",
        lambda: pytest.fail("Prometheus adapter must not be created when disabled"),
    )
    try:
        assert isinstance(get_hybrid_search_telemetry(), NoOpHybridSearchTelemetry)
    finally:
        get_hybrid_search_telemetry.cache_clear()


def test_telemetry_dependency_is_application_scoped_when_enabled(monkeypatch) -> None:
    """A repeated dependency resolution reuses one collector-owning adapter."""

    created: list[object] = []

    class Adapter:
        pass

    get_hybrid_search_telemetry.cache_clear()
    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.get_settings",
        lambda: Settings(hybrid_search_telemetry_enabled=True),
    )
    monkeypatch.setattr(
        "app.services.hybrid_search_dependencies.PrometheusHybridSearchTelemetry",
        lambda: created.append(Adapter()) or created[-1],
    )
    try:
        assert get_hybrid_search_telemetry() is get_hybrid_search_telemetry()
        assert len(created) == 1
    finally:
        get_hybrid_search_telemetry.cache_clear()

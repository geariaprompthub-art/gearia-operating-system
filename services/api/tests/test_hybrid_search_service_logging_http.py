"""HTTP contracts for HybridSearchService lifecycle events."""

import json
import logging
import asyncio
import threading
from io import StringIO

import pytest
import httpx
import anyio
from fastapi.testclient import TestClient

from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.main import create_app
from app.services.hybrid_search_dependencies import get_hybrid_search_telemetry
from app.services.hybrid_search_telemetry import NoOpHybridSearchTelemetry
from app.services.reranking_provider_errors import RerankingProviderUnavailableError
from test_hybrid_search import _install_real_hybrid_path_fakes


def _capturing_logger() -> tuple[SafeStructuredLogger, StringIO, logging.Logger]:
    stream = StringIO()
    raw = logging.getLogger("hybrid-service-http-events")
    raw.handlers.clear()
    raw.setLevel(logging.INFO)
    raw.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    raw.addHandler(handler)
    return SafeStructuredLogger(raw), stream, raw


@pytest.mark.parametrize(
    ("provider_error", "invalid_response", "status_code", "service_terminal"),
    [
        (None, False, 200, "hybrid_search_completed"),
        (RerankingProviderUnavailableError("PROVIDER_PAYLOAD_13B2"), False, 503, "hybrid_search_failed"),
        (None, True, 502, "hybrid_search_failed"),
    ],
)
def test_real_hybrid_endpoint_emits_one_safe_service_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception | None,
    invalid_response: bool,
    status_code: int,
    service_terminal: str,
) -> None:
    """HTTP middleware and the real service share one correlation ID without payload logging."""

    logger, stream, raw = _capturing_logger()
    _install_real_hybrid_path_fakes(monkeypatch, provider_error, invalid_response)
    monkeypatch.setattr("app.services.hybrid_search_dependencies.get_structured_logger", lambda _: logger)
    app = create_app(structured_logger=logger)
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: NoOpHybridSearchTelemetry()
    try:
        response = TestClient(app).post(
            "/search/hybrid",
            json={"query": "PRIVATE_QUERY_CONTENT_13B2", "top_k": 2},
            headers={"X-Request-ID": "service-http-13b2"},
        )
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        app.dependency_overrides.clear()
        raw.handlers.clear()

    assert response.status_code == status_code
    names = [event["event"] for event in events]
    assert names[0:2] == ["http_request_started", "hybrid_search_started"]
    http_terminal = "http_request_completed" if status_code < 500 else "http_request_failed"
    assert names[-2:] == [service_terminal, http_terminal]
    pipeline_events = [event for event in events if event["event"].startswith("hybrid_pipeline_stage_")]
    assert pipeline_events
    assert {event["request_id"] for event in events} == {"service-http-13b2"}
    emitted = "\n".join(json.dumps(event) for event in events)
    assert "PRIVATE_QUERY_CONTENT_13B2" not in emitted
    assert "PROVIDER_PAYLOAD_13B2" not in emitted


def test_fifty_overlapping_hybrid_requests_keep_service_events_correlated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ASGI batch proves the service inherits, but never creates, request context."""

    logger, stream, raw = _capturing_logger()
    _install_real_hybrid_path_fakes(monkeypatch)
    monkeypatch.setattr("app.services.hybrid_search_dependencies.get_structured_logger", lambda _: logger)
    request_count = 50
    barrier = threading.Barrier(request_count, timeout=5)
    original_run = __import__("app.services.hybrid_reranking_pipeline", fromlist=["HybridRerankingPipeline"]).HybridRerankingPipeline.run

    def synchronized_run(instance: object, *args: object, **kwargs: object) -> object:
        barrier.wait()
        return original_run(instance, *args, **kwargs)

    monkeypatch.setattr("app.services.hybrid_reranking_pipeline.HybridRerankingPipeline.run", synchronized_run)
    app = create_app(structured_logger=logger)
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: NoOpHybridSearchTelemetry()

    async def run_all() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous_tokens = limiter.total_tokens
        limiter.total_tokens = request_count
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await asyncio.gather(*[
                    client.post("/search/hybrid", json={"query": "query", "top_k": 2}, headers={"X-Request-ID": f"hybrid-concurrent-{index:03d}"})
                    for index in range(request_count)
                ])
        finally:
            limiter.total_tokens = previous_tokens

    try:
        responses = asyncio.run(run_all())
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        app.dependency_overrides.clear()
        raw.handlers.clear()

    grouped: dict[str, list[str]] = {}
    for event in events:
        grouped.setdefault(event["request_id"], []).append(event["event"])
    assert [response.status_code for response in responses] == [200] * request_count
    assert len(grouped) == request_count
    assert all(values[:2] == ["http_request_started", "hybrid_search_started"] for values in grouped.values())
    assert all(values[-2:] == ["hybrid_search_completed", "http_request_completed"] for values in grouped.values())
    assert all(values.count("hybrid_pipeline_stage_started") == 8 for values in grouped.values())
    assert all(values.count("hybrid_pipeline_stage_completed") == 8 for values in grouped.values())


def test_real_hybrid_endpoint_emits_pipeline_hydration_failure_before_preserved_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline failure adds one safe stage terminal event without changing the 500 contract."""

    logger, stream, raw = _capturing_logger()
    _install_real_hybrid_path_fakes(monkeypatch, hydration_error=True)
    monkeypatch.setattr("app.services.hybrid_search_dependencies.get_structured_logger", lambda _: logger)
    app = create_app(structured_logger=logger)
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: NoOpHybridSearchTelemetry()
    try:
        response = TestClient(app).post("/search/hybrid", json={"query": "query"})
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        app.dependency_overrides.clear()
        raw.handlers.clear()

    assert response.status_code == 500
    failed = [event for event in events if event["event"] == "hybrid_pipeline_stage_failed"]
    assert len(failed) == 1
    assert failed[0]["stage"] == "public_hydration"
    assert failed[0]["error_type"] == "RerankingPipelineHydrationError"

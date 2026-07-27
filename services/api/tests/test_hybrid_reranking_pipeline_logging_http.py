"""Dedicated HTTP coverage for pipeline-owned structured stage events."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.hybrid_search_dependencies import get_hybrid_search_telemetry
from app.services.hybrid_search_telemetry import NoOpHybridSearchTelemetry
from test_hybrid_search import _install_real_hybrid_path_fakes
from test_hybrid_search_service_logging_http import _capturing_logger


def test_pipeline_http_success_emits_complete_ordered_stage_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real endpoint exposes no logging details while all pipeline stages are traceable internally."""

    logger, stream, raw = _capturing_logger()
    _install_real_hybrid_path_fakes(monkeypatch)
    monkeypatch.setattr("app.services.hybrid_search_dependencies.get_structured_logger", lambda _: logger)
    app = create_app(structured_logger=logger)
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: NoOpHybridSearchTelemetry()
    try:
        response = TestClient(app).post(
            "/search/hybrid",
            json={"query": "PIPELINE_QUERY_SECRET", "top_k": 2},
            headers={"X-Request-ID": "pipeline-http-13b3"},
        )
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        app.dependency_overrides.clear()
        raw.handlers.clear()

    stages = [event for event in events if event["event"].startswith("hybrid_pipeline_stage_")]
    assert response.status_code == 200
    assert len(stages) == 16
    assert all(event["request_id"] == "pipeline-http-13b3" for event in stages)
    assert "PIPELINE_QUERY_SECRET" not in "\n".join(json.dumps(event) for event in stages)

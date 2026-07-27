"""HTTP integration proof that observation failures never change hybrid contracts."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.core.structured_logging import SafeStructuredLogger
from app.main import create_app
from app.services.hybrid_search_dependencies import get_hybrid_search_telemetry
from app.services.hybrid_search_telemetry import NoOpHybridSearchTelemetry
from app.services.reranking_provider_errors import (
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)
from test_hybrid_search import FailingTelemetry, _install_real_hybrid_path_fakes


class _BrokenLogger:
    """Fails after SafeStructuredLogger has entered its protected boundary."""

    def log(self, *_: object, **__: object) -> None:
        raise RuntimeError("logger failure")


def _logger(*, failing: bool) -> SafeStructuredLogger:
    if failing:
        return SafeStructuredLogger(_BrokenLogger())  # type: ignore[arg-type]
    raw = logging.getLogger(f"http-fail-open-{id(object())}")
    raw.handlers.clear()
    raw.addHandler(logging.NullHandler())
    raw.propagate = False
    return SafeStructuredLogger(raw)


@pytest.mark.parametrize("logger_fails,telemetry_fails", [(False, False), (True, False), (False, True), (True, True)])
def test_hybrid_http_success_is_identical_when_logger_or_telemetry_fails(
    monkeypatch: pytest.MonkeyPatch, logger_fails: bool, telemetry_fails: bool
) -> None:
    """The real DI/service/pipeline path is unchanged by either observation failure."""

    calls = _install_real_hybrid_path_fakes(monkeypatch)
    app = create_app(structured_logger=_logger(failing=logger_fails))
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: FailingTelemetry() if telemetry_fails else NoOpHybridSearchTelemetry()
    try:
        response = TestClient(app).post("/search/hybrid", json={"query": "query", "top_k": 2})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(calls) == 1 and len(calls[0]) == 2
    assert "logger failure" not in response.text and "telemetry" not in response.text.lower()


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (RerankingProviderUnavailableError("secret"), 503, "Reranking service is temporarily unavailable"),
        (RerankingProviderResponseError("secret"), 502, "Reranking service returned an invalid response"),
    ],
)
def test_hybrid_http_provider_contract_survives_simultaneous_observability_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status_code: int, detail: str
) -> None:
    """Functional provider errors win over failures in both logging and telemetry."""

    calls = _install_real_hybrid_path_fakes(monkeypatch, provider_error=error)
    app = create_app(structured_logger=_logger(failing=True))
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: FailingTelemetry()
    try:
        response = TestClient(app).post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert len(calls) == 1
    assert "secret" not in response.text and "telemetry" not in response.text.lower()


def test_hybrid_http_hydration_500_survives_simultaneous_observability_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing sanitized 500 hydration contract is also observation-independent."""

    calls = _install_real_hybrid_path_fakes(monkeypatch, hydration_error=True)
    app = create_app(structured_logger=_logger(failing=True))
    app.dependency_overrides[get_hybrid_search_telemetry] = lambda: FailingTelemetry()
    try:
        response = TestClient(app).post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {"detail": "Search result hydration failed"}
    assert len(calls) == 1
    assert "telemetry" not in response.text.lower()

"""Composition tests for isolated HTTP applications."""

import json
import logging
from io import StringIO

from fastapi.testclient import TestClient

from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.main import app, create_app


def _capturing_logger(name: str) -> tuple[SafeStructuredLogger, StringIO, logging.Logger]:
    """Build an application-local structured logger and capture its final JSON."""

    stream = StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    logger.addHandler(handler)
    return SafeStructuredLogger(logger), stream, logger


def test_default_module_app_and_factory_preserve_health_contract() -> None:
    """The ASGI entrypoint and a fresh composition expose the same health route."""

    assert TestClient(app).get("/health").json() == {"status": "ok"}
    assert TestClient(create_app()).get("/health").json() == {"status": "ok"}


def test_factory_uses_the_injected_logger_without_leaking_to_another_app() -> None:
    """A supplied logger belongs only to its own application instance."""

    logger, stream, raw_logger = _capturing_logger("factory-injected")
    isolated = create_app(structured_logger=logger)
    try:
        response = TestClient(isolated).get("/health", headers={"X-Request-ID": "factory-test"})
        payloads = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        raw_logger.handlers.clear()

    assert response.status_code == 200
    assert [payload["event"] for payload in payloads] == [
        "http_request_started",
        "http_request_completed",
    ]
    assert {payload["request_id"] for payload in payloads} == {"factory-test"}
    assert create_app().dependency_overrides == {}

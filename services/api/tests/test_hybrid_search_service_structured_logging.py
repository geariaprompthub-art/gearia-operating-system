"""Lifecycle logging contracts for the HybridSearchService."""

import logging
from uuid import uuid4

import pytest

from app.core.correlation import correlation_context
from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.repositories.lexical_search_repository import LexicalSearchCandidate
from app.repositories.vector_search_repository import VectorSearchCandidate
from app.services.graph_candidate_aggregator import GraphExpandedCandidate
from app.services.hybrid_search_service import HybridSearchService


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _Lexical:
    def __init__(self, content_id: object) -> None:
        self.content_id = content_id
        self.calls = 0

    def search(self, _: str, __: int) -> list[LexicalSearchCandidate]:
        self.calls += 1
        return [LexicalSearchCandidate(self.content_id)]  # type: ignore[arg-type]


class _Vector:
    def __init__(self, content_id: object) -> None:
        self.content_id = content_id
        self.calls = 0

    def search(self, _: str, __: int, ___: float) -> list[VectorSearchCandidate]:
        self.calls += 1
        return [VectorSearchCandidate(self.content_id, 0.9)]  # type: ignore[arg-type]


class _Graph:
    def __init__(self) -> None:
        self.calls = 0

    def expand(self, _: list[object]) -> list[GraphExpandedCandidate]:
        self.calls += 1
        return []


class _Pipeline:
    def __init__(self, result: dict[str, object] | Exception) -> None:
        self.result = result
        self.calls = 0

    def run(self, *_: object, **__: object) -> dict[str, object]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _logger() -> tuple[SafeStructuredLogger, _RecordingHandler, logging.Logger]:
    raw = logging.getLogger(f"hybrid-service-log-{uuid4()}")
    raw.handlers.clear()
    raw.setLevel(logging.INFO)
    raw.propagate = False
    handler = _RecordingHandler()
    raw.addHandler(handler)
    return SafeStructuredLogger(raw), handler, raw


def _service(result: dict[str, object] | Exception, logger: SafeStructuredLogger) -> tuple[HybridSearchService, _Pipeline]:
    first, second = uuid4(), uuid4()
    pipeline = _Pipeline(result)
    return (
        HybridSearchService(_Lexical(first), _Vector(second), _Graph(), pipeline, structured_logger=logger),  # type: ignore[arg-type]
        pipeline,
    )


def test_service_emits_only_started_and_completed_with_safe_metadata() -> None:
    """Success creates one terminal domain event and omits user data entirely."""

    logger, handler, raw = _logger()
    token = correlation_context.set("request-13b2")
    try:
        service, pipeline = _service({"items": [], "total": 0}, logger)
        result = service.search("PRIVATE_QUERY_CONTENT_13B2", 7)
    finally:
        correlation_context.reset(token)
        raw.handlers.clear()

    assert result == {"items": [], "total": 0}
    assert pipeline.calls == 1
    assert [record.event for record in handler.records] == [LogEvent.HYBRID_SEARCH_STARTED, LogEvent.HYBRID_SEARCH_COMPLETED]
    assert all(record.request_id == "request-13b2" for record in handler.records)
    fields = handler.records[-1].structured_fields
    assert isinstance(fields["duration_ms"], float) and fields["duration_ms"] >= 0
    assert fields["result_count"] == 0
    assert "PRIVATE_QUERY_CONTENT_13B2" not in str(fields)


def test_service_logs_safe_failure_once_and_reraises_the_original_exception() -> None:
    """Failure logging never exposes an exception message or replaces its identity."""

    logger, handler, raw = _logger()
    error = RuntimeError("PROVIDER_PAYLOAD_13B2")
    token = correlation_context.set("failure-13b2")
    try:
        service, pipeline = _service(error, logger)
        with pytest.raises(RuntimeError) as raised:
            service.search("PRIVATE_QUERY_CONTENT_13B2", 7)
    finally:
        correlation_context.reset(token)
        raw.handlers.clear()

    assert raised.value is error and pipeline.calls == 1
    assert [record.event for record in handler.records] == [LogEvent.HYBRID_SEARCH_STARTED, LogEvent.HYBRID_SEARCH_FAILED]
    fields = handler.records[-1].structured_fields
    assert fields["error_type"] == "RuntimeError"
    assert "PROVIDER_PAYLOAD_13B2" not in str(fields)


def test_safe_logger_failures_do_not_change_service_result_or_exception() -> None:
    """The service relies on SafeStructuredLogger's single fail-open boundary."""

    class BrokenLogger:
        def log(self, *_: object, **__: object) -> None:
            raise RuntimeError("logging unavailable")

    safe = SafeStructuredLogger(BrokenLogger())  # type: ignore[arg-type]
    successful, _ = _service({"items": [], "total": 0}, safe)
    failing_error = ValueError("DOCUMENT_SECRET_13B2")
    failing, _ = _service(failing_error, safe)

    assert successful.search("PROMPT_SECRET_13B2", 1) == {"items": [], "total": 0}
    with pytest.raises(ValueError) as raised:
        failing.search("VECTOR_SECRET_13B2", 1)
    assert raised.value is failing_error


@pytest.mark.parametrize("event", [LogEvent.HYBRID_SEARCH_STARTED, LogEvent.HYBRID_SEARCH_COMPLETED, LogEvent.HYBRID_SEARCH_FAILED])
def test_each_lifecycle_logging_failure_is_fail_open(event: str) -> None:
    """Every individual lifecycle emission is protected by SafeStructuredLogger."""

    class SelectivelyBrokenLogger:
        def log(self, _: object, __: object, *, extra: dict[str, object]) -> None:
            if extra["event"] == event:
                raise RuntimeError("logging unavailable")

    safe = SafeStructuredLogger(SelectivelyBrokenLogger())  # type: ignore[arg-type]
    if event == LogEvent.HYBRID_SEARCH_FAILED:
        error = RuntimeError("PROVIDER_PAYLOAD_13B2")
        service, _ = _service(error, safe)
        with pytest.raises(RuntimeError) as raised:
            service.search("query", 1)
        assert raised.value is error
    else:
        service, _ = _service({"items": [], "total": 0}, safe)
        assert service.search("query", 1) == {"items": [], "total": 0}

"""Stage-lifecycle structured logging for the reranking pipeline."""

import logging
from uuid import uuid4

import pytest

from app.core.correlation import correlation_context
from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.services.hybrid_reranking_pipeline import HybridRerankingPipeline
from app.services.rerank_document_formatter import RerankDocumentFormatter
from app.services.reranking_contracts import ProviderRerankResult
from app.services.reranking_provider_errors import RerankingProviderUnavailableError
from app.services.reranking_service import RerankingService
from test_hybrid_reranking_pipeline import (
    Documents,
    Eligibility,
    PoolSpy,
    Provider,
    PublicHydration,
    _document,
    _pool_candidate,
    _public,
    _rrf,
)


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _logger() -> tuple[SafeStructuredLogger, _RecordingHandler, logging.Logger]:
    raw = logging.getLogger(f"pipeline-stage-log-{uuid4()}")
    raw.handlers.clear()
    raw.setLevel(logging.INFO)
    raw.propagate = False
    handler = _RecordingHandler()
    raw.addHandler(handler)
    return SafeStructuredLogger(raw), handler, raw


def _pipeline(provider: Provider, content_id: object, logger: SafeStructuredLogger) -> HybridRerankingPipeline:
    return HybridRerankingPipeline(
        Eligibility(),
        Documents([_document(content_id)]),  # type: ignore[arg-type]
        RerankDocumentFormatter(),
        RerankingService(provider),
        PublicHydration([_public(content_id)]),  # type: ignore[arg-type]
        PoolSpy,
        structured_logger=logger,
    )


def test_pipeline_logs_each_executed_stage_once_with_safe_counts_and_request_context() -> None:
    """A successful run emits start/completion pairs without query or document data."""

    content_id = uuid4()
    PoolSpy.reset([_pool_candidate(content_id)])
    logger, handler, raw = _logger()
    pipeline = _pipeline(Provider([ProviderRerankResult(content_id, 1.0)]), content_id, logger)
    token = correlation_context.set("pipeline-request-13b3")
    try:
        assert pipeline.run("PIPELINE_QUERY_SECRET", [_rrf(content_id)], [], 1, 1)["total"] == 1
    finally:
        correlation_context.reset(token)
        raw.handlers.clear()

    events = [(record.event, record.structured_fields["stage"]) for record in handler.records]
    stages = [
        "candidate_pool", "eligibility", "reranking_hydration", "document_formatting",
        "provider_reranking", "final_top_k", "public_hydration", "response_building",
    ]
    assert events == [item for stage in stages for item in ((LogEvent.HYBRID_PIPELINE_STAGE_STARTED, stage), (LogEvent.HYBRID_PIPELINE_STAGE_COMPLETED, stage))]
    assert all(record.request_id == "pipeline-request-13b3" for record in handler.records)
    assert all(isinstance(record.structured_fields.get("duration_ms"), float) and record.structured_fields["duration_ms"] >= 0 for record in handler.records if record.event == LogEvent.HYBRID_PIPELINE_STAGE_COMPLETED)
    assert "PIPELINE_QUERY_SECRET" not in str([record.structured_fields for record in handler.records])


def test_pipeline_logs_only_provider_stage_failure_and_reraises_original_error() -> None:
    """A propagating stage error creates one terminal failure record with a safe type."""

    content_id = uuid4()
    PoolSpy.reset([_pool_candidate(content_id)])
    error = RerankingProviderUnavailableError("PIPELINE_PROVIDER_SECRET")
    logger, handler, raw = _logger()
    pipeline = _pipeline(Provider(error=error), content_id, logger)
    try:
        with pytest.raises(RerankingProviderUnavailableError) as raised:
            pipeline.run("PIPELINE_QUERY_SECRET", [_rrf(content_id)], [], 1, 1)
    finally:
        raw.handlers.clear()

    assert raised.value is error
    failures = [record for record in handler.records if record.event == LogEvent.HYBRID_PIPELINE_STAGE_FAILED]
    assert len(failures) == 1
    assert failures[0].structured_fields["stage"] == "provider_reranking"
    assert failures[0].structured_fields["error_type"] == "RerankingProviderUnavailableError"
    assert all(secret not in str([record.structured_fields for record in handler.records]) for secret in ("PIPELINE_QUERY_SECRET", "PIPELINE_PROVIDER_SECRET", "PIPELINE_DOCUMENT_SECRET", "PIPELINE_VECTOR_SECRET"))


def test_pipeline_stage_logger_is_fail_open_for_success_and_failure() -> None:
    """SafeStructuredLogger absorbs every stage emission failure without a fallback path."""

    class BrokenLogger:
        def log(self, *_: object, **__: object) -> None:
            raise RuntimeError("logging unavailable")

    content_id = uuid4()
    PoolSpy.reset([_pool_candidate(content_id)])
    safe = SafeStructuredLogger(BrokenLogger())  # type: ignore[arg-type]
    successful = _pipeline(Provider([ProviderRerankResult(content_id, 1.0)]), content_id, safe)
    assert successful.run("query", [_rrf(content_id)], [], 1, 1)["total"] == 1
    error = RerankingProviderUnavailableError("provider")
    failing = _pipeline(Provider(error=error), content_id, safe)
    with pytest.raises(RerankingProviderUnavailableError) as raised:
        failing.run("query", [_rrf(content_id)], [], 1, 1)
    assert raised.value is error

"""Prometheus adapter for the provider-neutral Hybrid Search telemetry contract."""

from prometheus_client import CollectorRegistry, Counter, Histogram, REGISTRY


class PrometheusHybridSearchTelemetry:
    """Emit only closed-vocabulary, low-cardinality Hybrid Search metrics."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self._requests = Counter("hybrid_search_requests", "Hybrid Search requests", ("status",), registry=registry)
        self._request_duration = Histogram("hybrid_search_request_duration_seconds", "Hybrid Search request duration", ("status",), registry=registry)
        self._stage_duration = Histogram("hybrid_search_stage_duration_seconds", "Hybrid Search stage duration", ("stage", "status"), registry=registry)
        self._stage_candidates = Histogram("hybrid_search_stage_candidates", "Hybrid Search candidate counts", ("stage", "direction"), registry=registry)
        self._provider_calls = Counter("hybrid_search_provider_calls", "Hybrid Search provider calls", ("status",), registry=registry)
        self._provider_candidates = Histogram("hybrid_search_provider_candidates", "Hybrid Search provider candidate batch sizes", registry=registry)

    def record_request_started(self) -> None:
        return None

    def record_request_completed(self, duration_seconds: float, final_item_count: int, status: str) -> None:
        self._requests.labels(status=status).inc()
        self._request_duration.labels(status=status).observe(max(0.0, duration_seconds))

    def record_stage_completed(self, stage: str, duration_seconds: float, *, input_count: int | None = None, output_count: int | None = None) -> None:
        self._stage_duration.labels(stage=stage, status="success").observe(max(0.0, duration_seconds))
        self._observe_counts(stage, input_count, output_count)

    def record_stage_failed(self, stage: str, duration_seconds: float, error_type: str) -> None:
        self._stage_duration.labels(stage=stage, status="error").observe(max(0.0, duration_seconds))

    def record_provider_call(self, duration_seconds: float, *, input_count: int, output_count: int, status: str) -> None:
        self._provider_calls.labels(status=status).inc()
        self._provider_candidates.observe(max(0, input_count))

    def _observe_counts(self, stage: str, input_count: int | None, output_count: int | None) -> None:
        if input_count is not None:
            self._stage_candidates.labels(stage=stage, direction="input").observe(max(0, input_count))
        if output_count is not None:
            self._stage_candidates.labels(stage=stage, direction="output").observe(max(0, output_count))

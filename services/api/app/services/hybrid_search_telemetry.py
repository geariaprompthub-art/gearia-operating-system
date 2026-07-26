"""Provider-neutral, fail-open observation contract for Hybrid Search."""

from typing import Protocol


class HybridSearchStage:
    """Closed stage vocabulary used by telemetry implementations."""

    LEXICAL_RETRIEVAL = "lexical_retrieval"
    VECTOR_RETRIEVAL = "vector_retrieval"
    RECIPROCAL_RANK_FUSION = "reciprocal_rank_fusion"
    GRAPH_EXPANSION = "graph_expansion"
    CANDIDATE_POOL = "candidate_pool"
    ELIGIBILITY = "eligibility"
    RERANKING_HYDRATION = "reranking_hydration"
    DOCUMENT_FORMATTING = "document_formatting"
    PROVIDER_RERANKING = "provider_reranking"
    FINAL_TOP_K = "final_top_k"
    PUBLIC_HYDRATION = "public_hydration"
    RESPONSE_BUILDING = "response_building"
    HYBRID_SEARCH_TOTAL = "hybrid_search_total"


class HybridSearchTelemetry(Protocol):
    """Observe Hybrid Search without exposing a metrics backend to business services."""

    def record_request_started(self) -> None: ...

    def record_request_completed(self, duration_seconds: float, final_item_count: int, status: str) -> None: ...

    def record_stage_completed(
        self,
        stage: str,
        duration_seconds: float,
        *,
        input_count: int | None = None,
        output_count: int | None = None,
    ) -> None: ...

    def record_stage_failed(self, stage: str, duration_seconds: float, error_type: str) -> None: ...

    def record_provider_call(
        self, duration_seconds: float, *, input_count: int, output_count: int, status: str
    ) -> None: ...


class NoOpHybridSearchTelemetry:
    """Telemetry implementation used when observation is disabled."""

    def record_request_started(self) -> None:
        return None

    def record_request_completed(self, duration_seconds: float, final_item_count: int, status: str) -> None:
        return None

    def record_stage_completed(
        self, stage: str, duration_seconds: float, *, input_count: int | None = None, output_count: int | None = None
    ) -> None:
        return None

    def record_stage_failed(self, stage: str, duration_seconds: float, error_type: str) -> None:
        return None

    def record_provider_call(
        self, duration_seconds: float, *, input_count: int, output_count: int, status: str
    ) -> None:
        return None


def emit_safely(callback: object, *args: object, **kwargs: object) -> None:
    """Contain telemetry failures so observation never changes functional control flow."""

    try:
        callback(*args, **kwargs)  # type: ignore[operator]
    except Exception:
        return None

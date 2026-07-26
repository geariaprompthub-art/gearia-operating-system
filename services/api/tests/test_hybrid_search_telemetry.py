"""Isolated contract coverage for Hybrid Search telemetry implementations."""

from prometheus_client import CollectorRegistry

from app.core.prometheus_hybrid_search_telemetry import PrometheusHybridSearchTelemetry
from app.services.hybrid_search_telemetry import NoOpHybridSearchTelemetry


def test_noop_telemetry_accepts_all_events_without_side_effects() -> None:
    telemetry = NoOpHybridSearchTelemetry()

    telemetry.record_request_started()
    telemetry.record_stage_completed("lexical_retrieval", 0.1, input_count=2, output_count=1)
    telemetry.record_stage_failed("graph_expansion", 0.2, "graph")
    telemetry.record_provider_call(0.3, input_count=1, output_count=1, status="success")
    telemetry.record_request_completed(0.4, final_item_count=1, status="success")


def test_prometheus_telemetry_records_only_low_cardinality_metrics_in_isolated_registry() -> None:
    registry = CollectorRegistry()
    telemetry = PrometheusHybridSearchTelemetry(registry=registry)

    telemetry.record_request_started()
    telemetry.record_stage_completed("lexical_retrieval", 0.1, input_count=3, output_count=2)
    telemetry.record_provider_call(0.2, input_count=2, output_count=2, status="success")
    telemetry.record_request_completed(0.3, final_item_count=1, status="success")

    all_samples = [sample for metric in registry.collect() for sample in metric.samples]
    samples = {sample.name: sample for sample in all_samples}
    assert samples["hybrid_search_requests_total"].labels == {"status": "success"}
    assert samples["hybrid_search_provider_calls_total"].labels == {"status": "success"}
    assert all(set(sample.labels) <= {"status", "stage", "direction", "le"} for sample in all_samples)
    assert sum(sample.value for sample in all_samples if sample.name == "hybrid_search_stage_candidates_sum") == 5
    assert samples["hybrid_search_provider_candidates_sum"].value == 2


def test_prometheus_telemetry_uses_independent_injected_registries_without_duplicate_series() -> None:
    """Collector ownership belongs to the injected registry, never to a request."""

    first_registry = CollectorRegistry()
    second_registry = CollectorRegistry()
    first = PrometheusHybridSearchTelemetry(registry=first_registry)
    second = PrometheusHybridSearchTelemetry(registry=second_registry)

    first.record_stage_failed("vector_retrieval", 0.1, "retrieval")
    second.record_stage_failed("graph_expansion", 0.2, "graph")

    first_labels = {
        label
        for metric in first_registry.collect()
        for sample in metric.samples
        for label in sample.labels
    }
    second_labels = {
        label
        for metric in second_registry.collect()
        for sample in metric.samples
        for label in sample.labels
    }
    assert first_labels | second_labels <= {"status", "stage", "direction", "le"}

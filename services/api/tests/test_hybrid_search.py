"""Hybrid retrieval service, HTTP contract and real PostgreSQL integration tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models.content import Content
from app.models.content_embedding import ContentEmbedding
from app.models.content_relationship import ContentRelationship
from app.models.source import Source
from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from app.repositories.content_hydration_repository import ContentHydrationRepository, HydratedContent
from app.repositories.lexical_search_repository import LexicalSearchCandidate, LexicalSearchRepository
from app.repositories.content_relationship_repository import ContentRelationshipRepository
from app.repositories.vector_search_repository import VectorSearchCandidate, VectorSearchRepository
from app.services.embedding_provider import build_content_embedding_text, content_hash
from app.services.fake_embedding_provider import FakeEmbeddingProvider
from app.services.hybrid_search_dependencies import get_hybrid_search_service
from app.services.hybrid_search_service import HybridSearchService, HybridSearchSettings
from app.services.graph_candidate_aggregator import GraphCandidateAggregator, GraphExpandedCandidate
from app.services.graph_expansion_service import GraphExpansionService
from app.services.vector_candidate_service import VectorCandidateService
from test_main import client


def _assert_public_safe(value: object) -> None:
    forbidden = {"embedding", "vector", "embedding_vector", "values", "rrf_score", "graph_score", "edge_score", "contributing_seed_ids", "relationship_id", "algorithm_version", "similarity", "distance", "search_rank", "provider", "model", "dimensions", "strategy"}
    if isinstance(value, dict):
        assert not (forbidden & set(value))
        for nested in value.values():
            _assert_public_safe(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_public_safe(nested)


class FakeLexicalRepository:
    def __init__(self, candidates: list[LexicalSearchCandidate], events: list[str], fail: bool = False) -> None:
        self.candidates = candidates; self.events = events; self.fail = fail; self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int) -> list[LexicalSearchCandidate]:
        self.events.append("lexical"); self.calls.append((query, limit))
        if self.fail: raise OperationalError("select", {}, Exception("offline"))
        return self.candidates


class FakeVectorCandidates:
    def __init__(self, candidates: list[VectorSearchCandidate], events: list[str], fail: bool = False) -> None:
        self.candidates = candidates; self.events = events; self.fail = fail; self.calls: list[tuple[str, int, float]] = []

    def search(self, query: str, candidate_k: int, threshold: float) -> list[VectorSearchCandidate]:
        self.events.append("vector"); self.calls.append((query, candidate_k, threshold))
        if self.fail: raise RuntimeError("provider unavailable")
        return self.candidates


class FakeHydrationRepository:
    def __init__(self, rows: list[HydratedContent], events: list[str], fail: bool = False) -> None:
        self.rows = rows; self.events = events; self.fail = fail; self.calls: list[list[UUID]] = []

    def hydrate(self, content_ids: list[UUID]) -> list[HydratedContent]:
        self.events.append("hydration"); self.calls.append(content_ids)
        if self.fail: raise RuntimeError("hydration unavailable")
        return self.rows


class FakeGraphExpansionService:
    def __init__(self, candidates: list[GraphExpandedCandidate], events: list[str], fail: bool = False) -> None:
        self.candidates = candidates; self.events = events; self.fail = fail; self.calls: list[list[UUID]] = []

    def expand(self, content_ids: list[UUID]) -> list[GraphExpandedCandidate]:
        self.events.append("graph"); self.calls.append(list(content_ids))
        if self.fail: raise RuntimeError("graph unavailable")
        return self.candidates


class FakeEligibilityRepository:
    def __init__(self, eligible_ids: list[UUID] | None, events: list[str], fail: bool = False) -> None:
        self.eligible_ids = eligible_ids; self.events = events; self.fail = fail; self.calls: list[list[UUID]] = []

    def filter_eligible(self, content_ids: list[UUID]) -> list[UUID]:
        self.events.append("eligibility"); self.calls.append(list(content_ids))
        if self.fail: raise RuntimeError("eligibility unavailable")
        return list(content_ids) if self.eligible_ids is None else self.eligible_ids


def _hydrated(content_id: UUID, title: str) -> HydratedContent:
    return HydratedContent(content_id=content_id, title=title, url=f"https://test/{title}", summary=None)


def _relationship(first_id: UUID, second_id: UUID, score: Decimal) -> ContentRelationship:
    """Build a valid deterministic relationship fixture in canonical UUID order."""

    content_id, related_content_id = sorted((first_id, second_id), key=str)
    return ContentRelationship(
        content_id=content_id,
        related_content_id=related_content_id,
        score=score,
        score_breakdown={},
        shared_topics=[],
        shared_keywords=[],
        same_category=False,
        same_source=False,
        text_similarity=Decimal("0"),
        published_distance_days=None,
        reasons=[],
        algorithm_version="deterministic-v1",
    )


def test_hybrid_service_fuses_sequentially_recalculates_ranks_and_hides_scores() -> None:
    shared, lexical_only, vector_only = uuid4(), uuid4(), uuid4()
    events: list[str] = []
    lexical = FakeLexicalRepository([LexicalSearchCandidate(lexical_only), LexicalSearchCandidate(shared)], events)
    vector = FakeVectorCandidates([VectorSearchCandidate(vector_only, 0.9), VectorSearchCandidate(shared, 0.8)], events)
    hydration = FakeHydrationRepository([_hydrated(shared, "shared"), _hydrated(vector_only, "vector")], events)
    graph = FakeGraphExpansionService([], events)
    eligibility = FakeEligibilityRepository(None, events)
    service = HybridSearchService(lexical, vector, hydration, graph, eligibility, HybridSearchSettings(lexical_candidate_k=1, vector_candidate_k=1, rrf_k=60))
    result = service.search("  hybrid query  ", top_k=3)
    assert events == ["lexical", "vector", "graph", "eligibility", "hydration"]
    assert lexical.calls == [("hybrid query", 3)] and vector.calls == [("hybrid query", 3, -1.0)]
    assert hydration.calls == [[shared, *sorted((lexical_only, vector_only))]]
    assert result["total"] == 2 and [item["rank"] for item in result["items"]] == [1, 2]
    assert result["items"][0]["matched_by"] == ["lexical", "vector"]
    assert result["items"][1]["matched_by"] == ["vector"]
    _assert_public_safe(result)


def test_hybrid_service_supports_lexical_only_vector_only_and_empty_results() -> None:
    lexical_id, vector_id = uuid4(), uuid4()
    for lexical_candidates, vector_candidates, rows, expected in [
        ([LexicalSearchCandidate(lexical_id)], [], [_hydrated(lexical_id, "lexical")], ["lexical"]),
        ([], [VectorSearchCandidate(vector_id, 0.9)], [_hydrated(vector_id, "vector")], ["vector"]),
        ([], [], [], []),
    ]:
        events: list[str] = []
        result = HybridSearchService(
            FakeLexicalRepository(lexical_candidates, events),
            FakeVectorCandidates(vector_candidates, events),
            FakeHydrationRepository(rows, events),
            FakeGraphExpansionService([], events),
            FakeEligibilityRepository(None, events),
        ).search("query", 20)
        assert [item["matched_by"][0] for item in result["items"]] == expected
        assert result["total"] == len(result["items"])


def test_hybrid_service_fails_closed_without_fallback() -> None:
    events: list[str] = []
    with pytest.raises(RuntimeError):
        HybridSearchService(FakeLexicalRepository([], events, fail=True), FakeVectorCandidates([], events), FakeHydrationRepository([], events), FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events)).search("query", 20)
    assert events == ["lexical"]
    events.clear()
    with pytest.raises(RuntimeError):
        HybridSearchService(FakeLexicalRepository([], events), FakeVectorCandidates([], events, fail=True), FakeHydrationRepository([], events), FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events)).search("query", 20)
    assert events == ["lexical", "vector"]


def test_hybrid_service_composes_graph_after_seeds_and_before_single_hydration() -> None:
    """Graph backfills only eligibility-created gaps and never changes seed provenance."""

    first, removed, third, graph, missing_after_eligibility = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    events: list[str] = []
    graph_candidates = [
        GraphExpandedCandidate(removed, Decimal("0.9"), (first,)),
        GraphExpandedCandidate(graph, Decimal("0.8"), (first,)),
        GraphExpandedCandidate(graph, Decimal("0.1"), (third,)),
        GraphExpandedCandidate(missing_after_eligibility, Decimal("0.7"), (third,)),
    ]
    graph_service = FakeGraphExpansionService(graph_candidates, events)
    eligibility = FakeEligibilityRepository([first, third, graph, missing_after_eligibility], events)
    hydration = FakeHydrationRepository([_hydrated(first, "first"), _hydrated(third, "third"), _hydrated(graph, "graph")], events)
    service = HybridSearchService(
        FakeLexicalRepository([LexicalSearchCandidate(first), LexicalSearchCandidate(removed), LexicalSearchCandidate(third)], events),
        FakeVectorCandidates([VectorSearchCandidate(first, 1.0), VectorSearchCandidate(removed, 0.9), VectorSearchCandidate(third, 0.8)], events),
        hydration,
        graph_service,
        eligibility,
        HybridSearchSettings(lexical_candidate_k=1, vector_candidate_k=1),
    )
    result = service.search("query", top_k=5)

    assert events == ["lexical", "vector", "graph", "eligibility", "hydration"]
    assert graph_service.calls == [[first, removed, third]]
    assert eligibility.calls == [[first, removed, third, graph, missing_after_eligibility]]
    assert hydration.calls == [[first, third, graph, missing_after_eligibility]]
    assert [item["content_id"] for item in result["items"]] == [first, third, graph]
    assert [item["rank"] for item in result["items"]] == [1, 2, 3]
    assert [item["matched_by"] for item in result["items"]] == [["lexical", "vector"], ["lexical", "vector"], ["graph"]]
    assert result["total"] == 3
    _assert_public_safe(result)


def test_hybrid_service_keeps_eligible_seeds_ahead_of_graph_and_applies_top_k_last() -> None:
    """Graph is called even with full RRF output but cannot displace eligible seeds."""

    first, second, graph = uuid4(), uuid4(), uuid4()
    events: list[str] = []
    graph_service = FakeGraphExpansionService([GraphExpandedCandidate(graph, Decimal("1"), (first,))], events)
    eligibility = FakeEligibilityRepository(None, events)
    hydration = FakeHydrationRepository([_hydrated(first, "first"), _hydrated(second, "second")], events)
    result = HybridSearchService(
        FakeLexicalRepository([LexicalSearchCandidate(first), LexicalSearchCandidate(second)], events),
        FakeVectorCandidates([], events),
        hydration,
        graph_service,
        eligibility,
    ).search("query", top_k=2)
    assert graph_service.calls == [[first, second]]
    assert eligibility.calls == [[first, second, graph]]
    assert hydration.calls == [[first, second]]
    assert [item["content_id"] for item in result["items"]] == [first, second]


def test_hybrid_service_handles_empty_graph_all_ineligible_and_failures_fail_closed() -> None:
    """Graph absence is normal, while Graph and eligibility errors never fall back."""

    seed = uuid4()
    events: list[str] = []
    empty_hydration = FakeHydrationRepository([], events)
    result = HybridSearchService(
        FakeLexicalRepository([LexicalSearchCandidate(seed)], events),
        FakeVectorCandidates([], events),
        empty_hydration,
        FakeGraphExpansionService([], events),
        FakeEligibilityRepository([], events),
    ).search("query", top_k=1)
    assert result == {"items": [], "total": 0} and empty_hydration.calls == [[]]

    for graph_fail, eligibility_fail in [(True, False), (False, True)]:
        events = []
        graph_service = FakeGraphExpansionService([], events, fail=graph_fail)
        eligibility = FakeEligibilityRepository(None, events, fail=eligibility_fail)
        hydration = FakeHydrationRepository([], events)
        with pytest.raises(RuntimeError):
            HybridSearchService(
                FakeLexicalRepository([LexicalSearchCandidate(seed)], events),
                FakeVectorCandidates([], events), hydration, graph_service, eligibility,
            ).search("query", top_k=1)
        if graph_fail:
            assert eligibility.calls == [] and hydration.calls == []
        else:
            assert len(eligibility.calls) == 1 and hydration.calls == []


def test_hybrid_service_can_return_only_eligible_graph_and_propagates_hydration_failure() -> None:
    """Graph-only backfill keeps its provenance, while hydration errors remain fail-closed."""

    seed, graph = uuid4(), uuid4()
    events: list[str] = []
    graph_service = FakeGraphExpansionService([GraphExpandedCandidate(graph, Decimal("1"), (seed,))], events)
    hydration = FakeHydrationRepository([_hydrated(graph, "graph")], events)
    result = HybridSearchService(
        FakeLexicalRepository([LexicalSearchCandidate(seed)], events),
        FakeVectorCandidates([], events), hydration, graph_service,
        FakeEligibilityRepository([graph], events),
    ).search("query", top_k=1)
    assert [item["content_id"] for item in result["items"]] == [graph]
    assert result["items"][0]["matched_by"] == ["graph"]

    events = []
    failing_hydration = FakeHydrationRepository([], events, fail=True)
    with pytest.raises(RuntimeError, match="hydration unavailable"):
        HybridSearchService(
            FakeLexicalRepository([LexicalSearchCandidate(seed)], events),
            FakeVectorCandidates([], events), failing_hydration,
            FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events),
        ).search("query", top_k=1)
    assert failing_hydration.calls == [[seed]]


class StubHybridService:
    def __init__(self, result: dict[str, object] | Exception) -> None:
        self.result = result; self.calls: list[tuple[str, int]] = []

    def search(self, query: str, top_k: int) -> dict[str, object]:
        self.calls.append((query, top_k))
        if isinstance(self.result, Exception): raise self.result
        return self.result


def test_hybrid_http_contract_validation_errors_and_safe_payloads() -> None:
    from app.main import app
    result = {"total": 1, "items": [{"rank": 1, "content_id": str(uuid4()), "title": "Hybrid", "url": "https://test/hybrid", "summary": None, "matched_by": ["lexical", "vector"]}]}
    stub = StubHybridService(result)
    app.dependency_overrides[get_hybrid_search_service] = lambda: stub
    try:
        success = client.post("/search/hybrid", json={"query": "  query  "})
        limit = client.post("/search/hybrid", json={"query": "x" * 8000, "top_k": 100})
        invalid = [
            client.post("/search/hybrid", json={"query": "   "}),
            client.post("/search/hybrid", json={"query": "x" * 8001}),
            client.post("/search/hybrid", json={"query": "query", "top_k": 0}),
            client.post("/search/hybrid", json={"query": "query", "top_k": 101}),
            client.post("/search/hybrid", json={"query": "query", "extra": "forbidden"}),
            client.post("/search/hybrid", json={"query": "query", "topk": 20}),
            client.post("/search/hybrid", json={"query": "query", "rrf_k": 60}),
            client.post("/search/hybrid", json={"query": "query", "vector_weight": 1}),
            client.post("/search/hybrid", json={"query": "query", "lexical_weight": 1}),
            client.post("/search/hybrid", json={"query": "query", "provider": "openai"}),
            client.post("/search/hybrid", json={"query": "query", "model": "text-embedding-3-small"}),
            client.post("/search/hybrid", json={"query": "query", "dimensions": 1536}),
            client.post("/search/hybrid", json={"query": "query", "top_k": True}),
            client.post("/search/hybrid", json={"query": "query", "top_k": 20.0}),
            client.post("/search/hybrid", json={"query": 42}),
            client.post("/search/hybrid", json=[]),
        ]
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)
    assert success.status_code == limit.status_code == 200 and stub.calls == [("query", 20), ("x" * 8000, 100)]
    assert success.json()["total"] == 1 and success.json()["items"][0]["rank"] == 1
    assert all(response.status_code == 422 for response in invalid)
    for response in [success, limit, *invalid]: _assert_public_safe(response.json())


def test_hybrid_http_sanitizes_known_and_unexpected_failures() -> None:
    from app.main import app
    for error, expected in [(RuntimeError("provider failure"), 503), (ValueError("unexpected internal"), 500)]:
        app.dependency_overrides[get_hybrid_search_service] = lambda error=error: StubHybridService(error)
        try:
            response = client.post("/search/hybrid", json={"query": "query"})
        finally:
            app.dependency_overrides.pop(get_hybrid_search_service, None)
        assert response.status_code == expected
        _assert_public_safe(response.json())


def test_hybrid_http_graph_pipeline_translates_failures_without_internal_details() -> None:
    """Every Graph-pipeline dependency failure is fail-closed at the HTTP boundary."""

    from app.main import app
    seed = uuid4()
    failure_factories = [
        lambda events: HybridSearchService(FakeLexicalRepository([], events, fail=True), FakeVectorCandidates([], events), FakeHydrationRepository([], events), FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events)),
        lambda events: HybridSearchService(FakeLexicalRepository([LexicalSearchCandidate(seed)], events), FakeVectorCandidates([], events, fail=True), FakeHydrationRepository([], events), FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events)),
        lambda events: HybridSearchService(FakeLexicalRepository([LexicalSearchCandidate(seed)], events), FakeVectorCandidates([], events), FakeHydrationRepository([], events), FakeGraphExpansionService([], events, fail=True), FakeEligibilityRepository(None, events)),
        lambda events: HybridSearchService(FakeLexicalRepository([LexicalSearchCandidate(seed)], events), FakeVectorCandidates([], events), FakeHydrationRepository([], events), FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events, fail=True)),
        lambda events: HybridSearchService(FakeLexicalRepository([LexicalSearchCandidate(seed)], events), FakeVectorCandidates([], events), FakeHydrationRepository([], events, fail=True), FakeGraphExpansionService([], events), FakeEligibilityRepository(None, events)),
    ]
    for factory in failure_factories:
        events: list[str] = []
        app.dependency_overrides[get_hybrid_search_service] = lambda factory=factory, events=events: factory(events)
        try:
            response = client.post("/search/hybrid", json={"query": "query"})
        finally:
            app.dependency_overrides.pop(get_hybrid_search_service, None)
        assert response.status_code == 503
        assert response.json() == {"detail": "Hybrid retrieval unavailable"}
        _assert_public_safe(response.json())

    app.dependency_overrides[get_hybrid_search_service] = lambda: StubHybridService(ValueError("internal score contract"))
    try:
        response = client.post("/search/hybrid", json={"query": "query"})
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)
    assert response.status_code == 500 and response.json() == {"detail": "Hybrid retrieval failed"}
    _assert_public_safe(response.json())


def test_hybrid_http_graph_backfill_uses_real_postgresql_and_rolls_back() -> None:
    """The public endpoint expands canonical edges only after ineligible seeds open space."""

    database = SessionLocal()
    marker = uuid4().hex
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    from app.main import app
    try:
        source = Source(name=f"sprint10-hybrid-graph-{marker}", type="manual")
        database.add(source)
        database.flush()
        seed_a_id, graph_shared_id, graph_ineligible_id, graph_second_id, seed_b_id = sorted(
            (uuid4(), uuid4(), uuid4(), uuid4(), uuid4()), key=str
        )
        seed_a = Content(id=seed_a_id, source_id=source.id, title="hybridseedtoken hybridseedtoken alpha", url=f"https://test/{marker}/seed-a", fingerprint=f"sprint10-{marker}-seed-a", processing_status="processed")
        graph_shared = Content(id=graph_shared_id, source_id=source.id, title="graph shared", url=f"https://test/{marker}/graph-shared", fingerprint=f"sprint10-{marker}-graph-shared", processing_status="processed")
        graph_ineligible = Content(id=graph_ineligible_id, source_id=source.id, title="graph pending", url=f"https://test/{marker}/graph-pending", fingerprint=f"sprint10-{marker}-graph-pending", processing_status="pending")
        graph_second = Content(id=graph_second_id, source_id=source.id, title="graph second", url=f"https://test/{marker}/graph-second", fingerprint=f"sprint10-{marker}-graph-second", processing_status="processed")
        seed_b = Content(id=seed_b_id, source_id=source.id, title="hybridseedtoken beta", url=f"https://test/{marker}/seed-b", fingerprint=f"sprint10-{marker}-seed-b", processing_status="pending")
        database.add_all([seed_a, graph_shared, graph_ineligible, graph_second, seed_b])
        database.flush()
        now = datetime.now(UTC)
        for content in (seed_a, seed_b):
            database.add(ContentEmbedding(content_id=content.id, embedding=[1.0] + [0.0] * 1535, content_hash=content_hash(build_content_embedding_text(content)), embedding_status="completed", embedded_at=now))
        database.add_all([
            _relationship(seed_a.id, graph_shared.id, Decimal("80")),
            _relationship(seed_b.id, graph_shared.id, Decimal("90")),
            _relationship(seed_a.id, graph_ineligible.id, Decimal("99")),
            _relationship(seed_b.id, graph_second.id, Decimal("70")),
        ])
        database.flush()

        provider = FakeEmbeddingProvider(vector_values=[1.0] + [0.0] * 1535)
        service = HybridSearchService(
            LexicalSearchRepository(database),
            VectorCandidateService(VectorSearchRepository(database), provider),
            ContentHydrationRepository(database),
            GraphExpansionService(ContentRelationshipRepository(database), GraphCandidateAggregator()),
            ContentEligibilityRepository(database),
        )
        app.dependency_overrides[get_hybrid_search_service] = lambda: service
        event.listen(database.bind, "before_cursor_execute", listener)
        try:
            response = client.post("/search/hybrid", json={"query": " hybridseedtoken ", "top_k": 3})
        finally:
            event.remove(database.bind, "before_cursor_execute", listener)

        assert response.status_code == 200 and provider.calls == 1
        payload = response.json()
        assert payload["total"] == len(payload["items"]) == 3
        assert [item["rank"] for item in payload["items"]] == [1, 2, 3]
        assert [item["content_id"] for item in payload["items"]] == [str(seed_a.id), str(graph_shared.id), str(graph_second.id)]
        assert [item["matched_by"] for item in payload["items"]] == [["lexical", "vector"], ["graph"], ["graph"]]
        assert str(seed_b.id) not in {item["content_id"] for item in payload["items"]}
        assert str(graph_ineligible.id) not in {item["content_id"] for item in payload["items"]}
        _assert_public_safe(payload)
        relationship_selects = [statement for statement in statements if "content_relationships" in statement and statement.lstrip().upper().startswith("SELECT")]
        eligibility_selects = [statement for statement in statements if "processing_status" in statement and "contents.id IN" in statement]
        assert len(relationship_selects) == 1 and len(eligibility_selects) == 1
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)

        repeat = client.post("/search/hybrid", json={"query": "hybridseedtoken", "top_k": 3})
        assert repeat.status_code == 200 and repeat.json() == payload
    finally:
        app.dependency_overrides.pop(get_hybrid_search_service, None)
        database.rollback()
        database.close()

    verification = SessionLocal()
    try:
        assert verification.scalar(select(Source).where(Source.name == f"sprint10-hybrid-graph-{marker}")) is None
    finally:
        verification.close()


def test_hybrid_endpoint_uses_real_postgresql_fts_pgvector_rrf_and_rollback() -> None:
    """Controlled endpoint integration validates all retrieval layers without residual data."""

    database = SessionLocal(); marker = uuid4().hex
    from app.main import app
    try:
        source = Source(name=f"sprint09-hybrid-{marker}", type="manual")
        database.add(source); database.flush()
        shared = Content(source_id=source.id, title="hybridtoken shared", url=f"https://test/{marker}/shared", fingerprint=f"hybrid-{marker}-shared", processing_status="processed")
        lexical_only = Content(source_id=source.id, title="hybridtoken lexical", url=f"https://test/{marker}/lexical", fingerprint=f"hybrid-{marker}-lexical", processing_status="processed")
        vector_only = Content(source_id=source.id, title="vector only", url=f"https://test/{marker}/vector", fingerprint=f"hybrid-{marker}-vector", processing_status="processed")
        database.add_all([shared, lexical_only, vector_only]); database.flush()
        now = datetime.now(UTC)
        for content, vector, digest in (
            (shared, [1.0] + [0.0] * 1535, content_hash(build_content_embedding_text(shared))),
            (lexical_only, [0.0, 1.0] + [0.0] * 1534, "0" * 64),
            (vector_only, [1.0] + [0.0] * 1535, content_hash(build_content_embedding_text(vector_only))),
        ):
            database.add(ContentEmbedding(content_id=content.id, embedding=vector, content_hash=digest, embedding_status="completed", embedded_at=now))
        database.flush()
        provider = FakeEmbeddingProvider(vector_values=[1.0] + [0.0] * 1535)
        service = HybridSearchService(
            LexicalSearchRepository(database),
            VectorCandidateService(VectorSearchRepository(database), provider),
            ContentHydrationRepository(database),
            GraphExpansionService(
                ContentRelationshipRepository(database),
                GraphCandidateAggregator(),
            ),
            ContentEligibilityRepository(database),
        )
        app.dependency_overrides[get_hybrid_search_service] = lambda: service
        try:
            response = client.post("/search/hybrid", json={"query": "hybridtoken", "top_k": 10})
        finally:
            app.dependency_overrides.pop(get_hybrid_search_service, None)
        assert response.status_code == 200 and provider.calls == 1
        payload = response.json()
        assert payload["total"] == 3 and payload["items"][0]["content_id"] == str(shared.id)
        assert payload["items"][0]["matched_by"] == ["lexical", "vector"]
        assert {item["matched_by"][0] for item in payload["items"][1:]} == {"lexical", "vector"}
        _assert_public_safe(payload)
    finally:
        database.rollback(); database.close()
    verification = SessionLocal()
    try:
        assert verification.query(Source).filter(Source.name == f"sprint09-hybrid-{marker}").count() == 0
    finally:
        verification.close()

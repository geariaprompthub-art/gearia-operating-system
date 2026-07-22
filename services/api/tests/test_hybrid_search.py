"""Hybrid retrieval service, HTTP contract and real PostgreSQL integration tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models.content import Content
from app.models.content_embedding import ContentEmbedding
from app.models.source import Source
from app.repositories.content_hydration_repository import ContentHydrationRepository, HydratedContent
from app.repositories.lexical_search_repository import LexicalSearchCandidate, LexicalSearchRepository
from app.repositories.vector_search_repository import VectorSearchCandidate, VectorSearchRepository
from app.services.embedding_provider import build_content_embedding_text, content_hash
from app.services.fake_embedding_provider import FakeEmbeddingProvider
from app.services.hybrid_search_dependencies import get_hybrid_search_service
from app.services.hybrid_search_service import HybridSearchService, HybridSearchSettings
from app.services.vector_candidate_service import VectorCandidateService
from test_main import client


def _assert_public_safe(value: object) -> None:
    forbidden = {"embedding", "vector", "embedding_vector", "values", "rrf_score", "similarity", "distance", "search_rank", "provider", "model", "dimensions", "strategy"}
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
    def __init__(self, rows: list[HydratedContent], events: list[str]) -> None:
        self.rows = rows; self.events = events; self.calls: list[list[UUID]] = []

    def hydrate(self, content_ids: list[UUID]) -> list[HydratedContent]:
        self.events.append("hydration"); self.calls.append(content_ids)
        return self.rows


def _hydrated(content_id: UUID, title: str) -> HydratedContent:
    return HydratedContent(content_id=content_id, title=title, url=f"https://test/{title}", summary=None)


def test_hybrid_service_fuses_sequentially_recalculates_ranks_and_hides_scores() -> None:
    shared, lexical_only, vector_only = uuid4(), uuid4(), uuid4()
    events: list[str] = []
    lexical = FakeLexicalRepository([LexicalSearchCandidate(lexical_only), LexicalSearchCandidate(shared)], events)
    vector = FakeVectorCandidates([VectorSearchCandidate(vector_only, 0.9), VectorSearchCandidate(shared, 0.8)], events)
    hydration = FakeHydrationRepository([_hydrated(shared, "shared"), _hydrated(vector_only, "vector")], events)
    service = HybridSearchService(lexical, vector, hydration, HybridSearchSettings(lexical_candidate_k=1, vector_candidate_k=1, rrf_k=60))
    result = service.search("  hybrid query  ", top_k=3)
    assert events == ["lexical", "vector", "hydration"]
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
        ).search("query", 20)
        assert [item["matched_by"][0] for item in result["items"]] == expected
        assert result["total"] == len(result["items"])


def test_hybrid_service_fails_closed_without_fallback() -> None:
    events: list[str] = []
    with pytest.raises(RuntimeError):
        HybridSearchService(FakeLexicalRepository([], events, fail=True), FakeVectorCandidates([], events), FakeHydrationRepository([], events)).search("query", 20)
    assert events == ["lexical"]
    events.clear()
    with pytest.raises(RuntimeError):
        HybridSearchService(FakeLexicalRepository([], events), FakeVectorCandidates([], events, fail=True), FakeHydrationRepository([], events)).search("query", 20)
    assert events == ["lexical", "vector"]


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

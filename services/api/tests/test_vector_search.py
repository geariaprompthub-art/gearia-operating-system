"""Deterministic unit and HTTP tests for Sprint 08 exact vector retrieval."""

from datetime import UTC, datetime
from math import isfinite, nan
from uuid import UUID, uuid4

from app.db import SessionLocal
from app.models.content import Content
from app.models.content_embedding import ContentEmbedding
from app.models.source import Source
from app.repositories.vector_search_repository import VectorSearchCandidate, VectorSearchRecord, VectorSearchRepository
from app.services.embedding_provider import build_content_embedding_text, content_hash
from app.services.fake_embedding_provider import FakeEmbeddingProvider
from app.services.vector_search_dependencies import get_vector_search_service
from app.services.vector_search_service import VectorSearchService
from test_main import client


def _assert_no_vector(value: object) -> None:
    """Reject vector-bearing fields recursively in public API output."""

    if isinstance(value, dict):
        assert not ({"embedding", "vector", "embedding_vector", "values"} & set(value))
        for nested in value.values():
            _assert_no_vector(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_vector(nested)


def _content(identifier: UUID | None = None, title: str = "Vector content") -> Content:
    """Build an in-memory content value suitable for deterministic service tests."""

    return Content(
        id=identifier or uuid4(),
        source_id=uuid4(),
        title=title,
        url=f"https://test/vector/{identifier or 'content'}",
        fingerprint=str(identifier or uuid4()),
        processing_status="processed",
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


def _candidate(content: Content, **overrides: object) -> VectorSearchCandidate:
    """Create a default eligible candidate, with targeted incompatibilities optional."""

    values: dict[str, object] = {
        "content": content,
        "content_hash": content_hash(build_content_embedding_text(content)),
        "status": "completed",
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dimensions": 1536,
        "text_strategy_version": "content-text-v1",
        "has_embedding": True,
    }
    values.update(overrides)
    return VectorSearchCandidate(**values)  # type: ignore[arg-type]


def _record(content: Content, similarity: float) -> VectorSearchRecord:
    """Build a repository result without carrying any vector value."""

    return VectorSearchRecord(
        content_id=content.id,
        source_id=content.source_id,
        title=content.title,
        url=content.url,
        summary=content.summary,
        author=content.author,
        published_at=content.published_at,
        language=content.language,
        category=content.category,
        topics=content.topics or [],
        keywords=content.keywords or [],
        relevance_score=content.relevance_score,
        processing_status=content.processing_status,
        created_at=content.created_at,
        similarity=similarity,
    )


class FakeVectorSearchRepository:
    """Repository double that records inputs and never touches a database."""

    def __init__(self, candidates: list[VectorSearchCandidate], records: list[VectorSearchRecord]) -> None:
        self.candidates = candidates
        self.records = records
        self.search_calls = 0
        self.last_eligible_ids: list[UUID] | None = None
        self.last_threshold: float | None = None
        self.last_top_k: int | None = None

    def eligible_candidates(self) -> list[VectorSearchCandidate]:
        return self.candidates

    def search(self, query_vector: list[float], eligible_ids: list[UUID], top_k: int, threshold: float) -> list[VectorSearchRecord]:
        self.search_calls += 1
        self.last_eligible_ids = eligible_ids
        self.last_threshold = threshold
        self.last_top_k = top_k
        matches = [
            record for record in self.records
            if record.content_id in eligible_ids and isfinite(record.similarity) and record.similarity >= threshold
        ]
        return sorted(matches, key=lambda record: (-record.similarity, record.content_id))[:top_k]


def test_vector_search_normalizes_query_ranks_results_and_calls_provider_once() -> None:
    """Ranks are contiguous, total equals items, and the ephemeral query is generated once."""

    first = _content(UUID("00000000-0000-0000-0000-000000000001"), "First")
    second = _content(UUID("00000000-0000-0000-0000-000000000002"), "Second")
    third = _content(UUID("00000000-0000-0000-0000-000000000003"), "Third")
    repository = FakeVectorSearchRepository(
        [_candidate(first), _candidate(second), _candidate(third)],
        [_record(second, 0.9), _record(first, 0.9), _record(third, nan)],
    )
    provider = FakeEmbeddingProvider()
    result = VectorSearchService(repository, provider).search("  automation  ", top_k=2, threshold=0.5)
    assert provider.calls == 1 and provider.inputs == ["automation"]
    assert repository.search_calls == 1 and repository.last_top_k == 2 and repository.last_threshold == 0.5
    assert result["query"] == "automation" and result["total"] == len(result["items"]) == 2
    assert [item["rank"] for item in result["items"]] == [1, 2]
    assert [item["content_id"] for item in result["items"]] == [first.id, second.id]
    empty = VectorSearchService(repository, FakeEmbeddingProvider()).search("automation", top_k=2, threshold=0.95)
    assert empty["total"] == 0 and empty["items"] == []
    _assert_no_vector(result)


def test_vector_search_excludes_ineligible_candidates_and_skips_empty_vector_query() -> None:
    """Failed, processing, stale and incompatible rows never reach vector SQL."""

    usable = _content(title="usable")
    candidates = [
        _candidate(usable),
        _candidate(_content(title="stale"), content_hash="0" * 64),
        _candidate(_content(title="failed"), status="failed"),
        _candidate(_content(title="processing"), status="processing"),
        _candidate(_content(title="provider"), provider="other"),
        _candidate(_content(title="model"), model="other"),
        _candidate(_content(title="strategy"), text_strategy_version="other"),
        _candidate(_content(title="dimensions"), dimensions=2),
        _candidate(_content(title="null-vector"), has_embedding=False),
    ]
    repository = FakeVectorSearchRepository(candidates, [_record(usable, 0.75)])
    result = VectorSearchService(repository, FakeEmbeddingProvider()).search("query", 20, 0.0)
    assert repository.last_eligible_ids == [usable.id] and result["total"] == 1
    empty_repository = FakeVectorSearchRepository([_candidate(_content(), status="failed")], [])
    empty = VectorSearchService(empty_repository, FakeEmbeddingProvider()).search("query", 20, 0.0)
    assert empty["items"] == [] and empty_repository.search_calls == 0


def test_vector_search_rejects_provider_failures_empty_and_invalid_vectors() -> None:
    """Provider failures cannot create a result or cause database persistence."""

    repository = FakeVectorSearchRepository([], [])
    for provider in (
        FakeEmbeddingProvider(fail=True),
        FakeEmbeddingProvider(dimensions=0),
        FakeEmbeddingProvider(dimensions=2),
        FakeEmbeddingProvider(vector_values=[nan] * 1536),
    ):
        try:
            VectorSearchService(repository, provider).search("query", 20, 0.0)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid provider result must fail")
    assert repository.search_calls == 0


def test_vector_search_http_contract_is_safe_and_validates_payloads() -> None:
    """The thin route uses an override and never exposes vectors in any response."""

    content = _content(title="HTTP")
    provider = FakeEmbeddingProvider()
    repository = FakeVectorSearchRepository([_candidate(content)], [_record(content, 0.8)])
    from app.main import app
    app.dependency_overrides[get_vector_search_service] = lambda: VectorSearchService(repository, provider)
    try:
        response = client.post("/search/vector", json={"query": "  HTTP query  ", "top_k": 1, "threshold": 0.5})
    finally:
        app.dependency_overrides.pop(get_vector_search_service, None)
    invalid_responses = [
        client.post("/search/vector", json={"query": "   "}),
        client.post("/search/vector", json={"query": "x", "top_k": 0}),
        client.post("/search/vector", json={"query": "x", "threshold": 2}),
    ]
    assert response.status_code == 200 and provider.calls == 1
    payload = response.json()
    assert payload["query"] == "HTTP query" and payload["total"] == 1 and payload["items"][0]["rank"] == 1
    assert all(item.status_code == 422 for item in invalid_responses)
    for item in [response, *invalid_responses]:
        _assert_no_vector(item.json())


def test_vector_search_http_sanitizes_provider_and_vector_failures() -> None:
    """Provider errors and invalid provider vectors become safe 503 responses."""

    from app.main import app
    for provider in (
        FakeEmbeddingProvider(fail=True),
        FakeEmbeddingProvider(dimensions=0),
        FakeEmbeddingProvider(dimensions=2),
        FakeEmbeddingProvider(vector_values=[nan] * 1536),
    ):
        repository = FakeVectorSearchRepository([], [])
        app.dependency_overrides[get_vector_search_service] = lambda provider=provider: VectorSearchService(repository, provider)
        try:
            response = client.post("/search/vector", json={"query": "query"})
        finally:
            app.dependency_overrides.pop(get_vector_search_service, None)
        assert response.status_code == 503 and response.json()["detail"] == "Vector search unavailable"
        _assert_no_vector(response.json())


def test_vector_search_http_input_matrix_and_extra_fields_policy() -> None:
    """HTTP validation freezes bounds, normalization, defaults and extra-field behavior."""

    content = _content(title="matrix")
    provider = FakeEmbeddingProvider()
    repository = FakeVectorSearchRepository([_candidate(content)], [_record(content, 1.0)])
    from app.main import app
    app.dependency_overrides[get_vector_search_service] = lambda: VectorSearchService(repository, provider)
    try:
        nominal = client.post("/search/vector", json={"query": " query ", "extra": "ignored"})
        top_one = client.post("/search/vector", json={"query": "query", "top_k": 1})
        top_hundred = client.post("/search/vector", json={"query": "query", "top_k": 100})
        threshold_values = [client.post("/search/vector", json={"query": "query", "threshold": value}) for value in (-1, 0, 1)]
        invalid = [
            client.post("/search/vector", json={"query": ""}),
            client.post("/search/vector", json={"query": "   "}),
            client.post("/search/vector", json={"query": "x" * 8001}),
            client.post("/search/vector", json={"query": "query", "top_k": 0}),
            client.post("/search/vector", json={"query": "query", "top_k": 101}),
            client.post("/search/vector", json={"query": "query", "threshold": -1.01}),
            client.post("/search/vector", json={"query": "query", "threshold": 1.01}),
            client.post("/search/vector", json=[]),
        ]
    finally:
        app.dependency_overrides.pop(get_vector_search_service, None)
    assert nominal.status_code == top_one.status_code == top_hundred.status_code == 200
    assert nominal.json()["query"] == "query" and nominal.json()["top_k"] == 20 and nominal.json()["threshold"] == 0.0
    assert top_one.json()["top_k"] == 1 and top_hundred.json()["top_k"] == 100
    assert all(response.status_code == 200 and response.json()["total"] == len(response.json()["items"]) for response in threshold_values)
    assert all(response.status_code == 422 for response in invalid)
    for response in [nominal, top_one, top_hundred, *threshold_values, *invalid]:
        _assert_no_vector(response.json())


def test_vector_search_repository_uses_pgvector_cosine_threshold_in_rolled_back_transaction() -> None:
    """Real PostgreSQL validates ordering, threshold and rollback without residual rows."""

    database = SessionLocal()
    try:
        marker = uuid4().hex
        source = Source(name=f"sprint08-{marker}", type="manual")
        database.add(source)
        database.flush()
        first = Content(source_id=source.id, title="identical", url=f"https://test/{marker}/one", fingerprint=f"one-{marker}", processing_status="processed")
        second = Content(source_id=source.id, title="near", url=f"https://test/{marker}/two", fingerprint=f"two-{marker}", processing_status="processed")
        third = Content(source_id=source.id, title="orthogonal", url=f"https://test/{marker}/three", fingerprint=f"three-{marker}", processing_status="processed")
        database.add_all([first, second, third])
        database.flush()
        now = datetime.now(UTC)
        vectors = ([1.0] + [0.0] * 1535, [0.8, 0.6] + [0.0] * 1534, [0.0, 1.0] + [0.0] * 1534)
        for content, vector in zip((first, second, third), vectors, strict=True):
            database.add(ContentEmbedding(content_id=content.id, embedding=vector, content_hash=content_hash(build_content_embedding_text(content)), embedding_status="completed", embedded_at=now))
        database.flush()
        repository = VectorSearchRepository(database)
        eligible_ids = [candidate.content.id for candidate in repository.eligible_candidates()]
        rows = repository.search([1.0] + [0.0] * 1535, eligible_ids, top_k=10, threshold=0.8)
        assert [row.title for row in rows] == ["identical", "near"]
        assert rows[0].similarity == 1.0 and rows[1].similarity >= 0.8
        assert all(isfinite(row.similarity) for row in rows)
    finally:
        database.rollback()
        database.close()
    verification = SessionLocal()
    try:
        assert verification.query(Source).filter(Source.name == f"sprint08-{marker}").count() == 0
    finally:
        verification.close()

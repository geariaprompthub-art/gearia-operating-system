"""Tests for API health and source endpoints."""

from collections.abc import Generator

from fastapi.testclient import TestClient
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models.content import Content
from app.models.source import Source
from app.services.enrichment_service import EnrichmentService, normalize_text

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)


def override_get_db() -> Generator[Session, None, None]:
    """Provide an isolated database session for a test request."""

    database = TestingSessionLocal()
    try:
        yield database
    finally:
        database.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def setup_function(_: object) -> None:
    """Reset the source table before each test."""

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def test_health_returns_ok() -> None:
    """The health endpoint reports the API as healthy."""

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_source_crud_routes() -> None:
    """The generated OpenAPI document exposes all required source routes."""

    paths = app.openapi()["paths"]

    assert {
        "/health",
        "/sources",
        "/sources/{source_id}",
        "/contents",
        "/contents/{content_id}",
        "/contents/{content_id}/enrich",
        "/scout/run",
        "/enrichment/run",
        "/search",
    }.issubset(paths)
    assert {"get", "post"}.issubset(paths["/sources"])
    assert {"get", "put", "delete"}.issubset(paths["/sources/{source_id}"])


def test_sources_returns_empty_list() -> None:
    """The source collection endpoint initially returns no records."""

    response = client.get("/sources")

    assert response.status_code == 200
    assert response.json() == []


def test_sources_can_be_created() -> None:
    """A source can be created through the API."""

    response = client.post(
        "/sources",
        json={"name": "manual-source", "type": "manual", "enabled": True},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "manual-source"
    assert response.json()["type"] == "manual"
    assert response.json()["enabled"] is True


def test_scout_run_persists_rss_content_without_duplicates(monkeypatch: object) -> None:
    """Scout persists normalized RSS entries once across repeated executions."""

    database = TestingSessionLocal()
    source = Source(
        name="Example RSS",
        type="rss",
        url="https://example.test/feed.xml",
        enabled=True,
    )
    database.add(source)
    database.commit()
    database.close()

    def fake_parse(_: str) -> dict[str, object]:
        return {
            "feed": {"language": "en"},
            "entries": [
                {
                    "title": "RSS item",
                    "link": "https://example.test/articles/1",
                    "summary": "A normalized feed item",
                    "author": "GearIA",
                    "published_parsed": (2026, 7, 20, 12, 0, 0, 0, 1, 0),
                }
            ],
        }

    monkeypatch.setattr("app.services.scout.feedparser.parse", fake_parse)
    monkeypatch.setattr(
        "app.services.scout.SafeRSSFetcher.fetch", lambda _self, _url: b"<rss></rss>"
    )

    first_run = client.post("/scout/run")
    second_run = client.post("/scout/run")
    contents = client.get("/contents")

    assert first_run.status_code == 200
    assert first_run.json() == {"sources_processed": 1, "contents_created": 1}
    assert second_run.status_code == 200
    assert second_run.json() == {"sources_processed": 1, "contents_created": 0}
    assert contents.status_code == 200
    assert len(contents.json()) == 1
    assert contents.json()[0]["title"] == "RSS item"
    assert contents.json()[0]["processing_status"] == "pending"
    database = TestingSessionLocal()
    fingerprint_count = database.scalar(select(func.count(Content.fingerprint)))
    distinct_fingerprint_count = database.scalar(select(func.count(func.distinct(Content.fingerprint))))
    database.close()
    assert fingerprint_count == distinct_fingerprint_count == 1


def test_normalization_classification_topics_keywords_and_score_are_deterministic() -> None:
    """Rule outputs are accent-insensitive, stable, unique, and bounded."""

    text = normalize_text("  Inteligência Artificial com ChatGPT  ", "OpenAI agents automation workflow")
    service = EnrichmentService(TestingSessionLocal())
    topics = service.identify_topics(text)
    keywords = service.extract_keywords(text * 5)
    score = service.calculate_relevance(text, "summary with openai", topics, datetime.now(UTC))

    assert text.startswith("inteligencia artificial com chatgpt")
    assert service.identify_category(text) == "automacao"
    assert {"chatgpt", "openai", "agentes", "automacao"}.issubset(topics)
    assert len(topics) == len(set(topics))
    assert len(keywords) == len(set(keywords)) <= 10
    assert 0 <= score <= 100


def test_category_priority_and_fallback() -> None:
    """Prompt rules take precedence and unmatched text falls back to outros."""

    service = EnrichmentService(TestingSessionLocal())

    assert service.identify_category(normalize_text("ChatGPT prompt engineering guide")) == "engenharia_de_prompt"
    assert service.identify_category(normalize_text("gardening and recipes")) == "outros"


def test_enrichment_run_processes_only_pending_and_manual_reprocessing_works() -> None:
    """Batch processing skips terminal states while manual processing can rerun one item."""

    database = TestingSessionLocal()
    pending = Content(source_id=_create_source(database).id, title="ChatGPT automation", url="https://test/pending", summary="OpenAI workflow", fingerprint="pending-fingerprint")
    processed = Content(source_id=pending.source_id, title="Old", url="https://test/processed", processing_status="processed", fingerprint="processed-fingerprint")
    failed = Content(source_id=pending.source_id, title="Failed", url="https://test/failed", processing_status="failed", fingerprint="failed-fingerprint")
    database.add_all([pending, processed, failed])
    database.commit()
    pending_id = pending.id
    processed_id = processed.id
    database.close()

    first_run = client.post("/enrichment/run")
    second_run = client.post("/enrichment/run")
    pending_response = client.get(f"/contents/{pending_id}")
    manual_response = client.post(f"/contents/{processed_id}/enrich")

    assert first_run.json() == {"contents_found": 1, "contents_processed": 1, "contents_failed": 0}
    assert second_run.json() == {"contents_found": 0, "contents_processed": 0, "contents_failed": 0}
    assert pending_response.json()["processing_status"] == "processed"
    assert pending_response.json()["processed_at"] is not None
    assert manual_response.status_code == 200
    assert manual_response.json()["processing_status"] == "processed"


def test_enrichment_failure_is_recorded_and_missing_content_returns_404(monkeypatch: object) -> None:
    """A failure changes only its item to failed and missing IDs return 404."""

    database = TestingSessionLocal()
    content = Content(source_id=_create_source(database).id, title="Break", url="https://test/break", fingerprint="break-fingerprint")
    database.add(content)
    database.commit()
    content_id = content.id
    database.close()

    def raise_error(_: str) -> str:
        raise ValueError("classification unavailable")

    monkeypatch.setattr(EnrichmentService, "identify_category", staticmethod(raise_error))
    response = client.post(f"/contents/{content_id}/enrich")
    missing = client.post("/contents/00000000-0000-0000-0000-000000000000/enrich")

    assert response.status_code == 200
    assert response.json()["processing_status"] == "failed"
    assert response.json()["processing_error"] == "classification unavailable"
    assert response.json()["processed_at"] is not None
    assert missing.status_code == 404


def test_content_filters_are_applied_in_database() -> None:
    """Category, topic, status, score, source and combined filters return matching rows."""

    database = TestingSessionLocal()
    source = _create_source(database)
    first = Content(
        source_id=source.id, title="AI", url="https://test/ai", fingerprint="ai-fingerprint", category="inteligencia_artificial",
        topics=["chatgpt", "openai"], language="en", processing_status="processed", relevance_score=90,
    )
    second = Content(
        source_id=source.id, title="Marketing", url="https://test/marketing", fingerprint="marketing-fingerprint", category="marketing",
        topics=["marketing"], language="pt", processing_status="pending", relevance_score=20,
    )
    database.add_all([first, second])
    database.commit()
    source_id = source.id
    database.close()

    assert len(client.get("/contents?category=inteligencia_artificial").json()) == 1
    assert len(client.get("/contents?topic=chatgpt").json()) == 1
    assert len(client.get("/contents?processing_status=processed").json()) == 1
    assert len(client.get("/contents?min_relevance_score=70").json()) == 1
    assert len(client.get(f"/contents?source_id={source_id}&category=marketing").json()) == 1
    assert client.get("/contents?min_relevance_score=90&max_relevance_score=10").status_code == 422


def test_search_endpoint_returns_paginated_results_and_validates_ranges() -> None:
    """Search accepts empty queries, applies pagination, and validates ranges."""

    database = TestingSessionLocal()
    source = _create_source(database)
    database.add_all([
        Content(source_id=source.id, title="ChatGPT guide", url="https://test/search-1", fingerprint="search-1", summary="automation workflow", category="inteligencia_artificial", topics=["chatgpt", "automacao"], keywords=["openai"], relevance_score=90, language="en", processing_status="processed"),
        Content(source_id=source.id, title="Other content", url="https://test/search-2", fingerprint="search-2", summary="chatgpt appears in summary", category="tecnologia", topics=["chatgpt"], keywords=["software"], relevance_score=50, language="pt", processing_status="pending"),
        Content(source_id=source.id, title="Marketing", url="https://test/search-3", fingerprint="search-3", category="marketing", topics=["marketing"], keywords=["growth"], relevance_score=10, language="pt", processing_status="processed"),
    ])
    database.commit()
    database.close()

    empty = client.get("/search?page=1&page_size=2")
    searched = client.get("/search?q=CHATGPT&category=inteligencia_artificial&processing_status=processed&min_relevance_score=70")
    topic = client.get("/search?topic=chatgpt")
    invalid_score = client.get("/search?min_relevance_score=90&max_relevance_score=20")
    invalid_dates = client.get("/search?published_from=2026-02-01T00:00:00Z&published_to=2026-01-01T00:00:00Z")
    invalid_page = client.get("/search?page=0")
    invalid_size = client.get("/search?page_size=101")

    assert empty.status_code == 200
    assert empty.json()["total"] == 3
    assert empty.json()["total_pages"] == 2
    assert len(empty.json()["items"]) == 2
    assert empty.json()["items"][0]["search_rank"] is None
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["title"] == "ChatGPT guide"
    assert len(topic.json()["items"]) == 2
    assert invalid_score.status_code == invalid_dates.status_code == invalid_page.status_code == invalid_size.status_code == 422


def test_search_filters_dates_sorting_and_empty_results() -> None:
    """Search combines SQL filters and supports safe ordering modes."""

    database = TestingSessionLocal()
    source = _create_source(database)
    old = Content(source_id=source.id, title="Old automation", url="https://test/old", fingerprint="old-search", category="automacao", topics=["automacao"], keywords=["workflow"], relevance_score=20, processing_status="processed", published_at=datetime(2025, 1, 1, tzinfo=UTC))
    recent = Content(source_id=source.id, title="Recent automation", url="https://test/recent", fingerprint="recent-search", category="automacao", topics=["automacao"], keywords=["workflow"], relevance_score=80, processing_status="processed", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    database.add_all([old, recent])
    database.commit()
    database.close()

    filtered = client.get("/search?q=automation&topic=automacao&processing_status=processed&min_relevance_score=50&published_from=2025-12-01T00:00:00Z&sort_by=relevance&sort_order=desc")
    by_date = client.get("/search?sort_by=published_at&sort_order=asc")
    beyond = client.get("/search?page=99&page_size=2")
    none = client.get("/search?q=termoabsolutamenteinexistente987654")
    invalid_sort = client.get("/search?sort_by=unsafe")

    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["title"] == "Recent automation"
    assert by_date.json()["items"][0]["title"] == "Old automation"
    assert beyond.status_code == 200 and beyond.json()["items"] == [] and beyond.json()["total"] == 2
    assert none.status_code == 200 and none.json()["total"] == 0 and none.json()["total_pages"] == 0
    assert invalid_sort.status_code == 422


def test_search_matches_every_indexed_field_and_title_has_higher_rank() -> None:
    """Text search covers title, summary, keywords, topics and category with weighted rank."""

    database = TestingSessionLocal()
    source = _create_source(database)
    database.add_all([
        Content(source_id=source.id, title="needle title", url="https://test/title", fingerprint="field-title", processing_status="processed"),
        Content(source_id=source.id, title="summary", summary="needle summary", url="https://test/summary", fingerprint="field-summary", processing_status="processed"),
        Content(source_id=source.id, title="keywords", keywords=["needlekeyword"], url="https://test/keywords", fingerprint="field-keyword", processing_status="processed"),
        Content(source_id=source.id, title="topics", topics=["needletopic"], url="https://test/topics", fingerprint="field-topic", processing_status="processed"),
        Content(source_id=source.id, title="category", category="needlecategory", url="https://test/category", fingerprint="field-category", processing_status="processed"),
    ])
    database.commit()
    database.close()

    title_results = client.get("/search?q=needle&sort_by=rank&sort_order=desc").json()
    summary = client.get("/search?q=needle%20summary").json()
    keyword = client.get("/search?q=needlekeyword").json()
    topic = client.get("/search?q=needletopic").json()
    category = client.get("/search?q=needlecategory&category=needlecategory").json()

    assert title_results["items"][0]["title"] == "needle title"
    assert title_results["items"][0]["search_rank"] > title_results["items"][1]["search_rank"]
    assert summary["total"] == keyword["total"] == topic["total"] == category["total"] == 1


def test_search_ordering_and_empty_query_normalization() -> None:
    """Whitelisted sorting works and blank query is treated as no textual query."""

    database = TestingSessionLocal()
    source = _create_source(database)
    database.add_all([
        Content(source_id=source.id, title="Low", url="https://test/low", fingerprint="sort-low", relevance_score=10, processing_status="processed"),
        Content(source_id=source.id, title="High", url="https://test/high", fingerprint="sort-high", relevance_score=90, processing_status="processed"),
    ])
    database.commit()
    database.close()

    ascending = client.get("/search?sort_by=relevance&sort_order=asc").json()
    descending = client.get("/search?sort_by=relevance&sort_order=desc").json()
    blank = client.get("/search?q=%20%20&sort_by=rank").json()
    invalid_order = client.get("/search?sort_order=sideways")

    assert ascending["items"][0]["title"] == "Low"
    assert descending["items"][0]["title"] == "High"
    assert blank["query"] is None and blank["items"][0]["search_rank"] is None
    assert invalid_order.status_code == 422


def _create_source(database: Session) -> Source:
    """Create a source suitable for direct content persistence tests."""

    source = Source(name=f"source-{database.scalar(select(func.count(Source.id)))}", type="manual")
    database.add(source)
    database.commit()
    database.refresh(source)
    return source

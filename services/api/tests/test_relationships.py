"""Tests for deterministic-v1 content relationships."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from app.models.content import Content
from app.models.content_relationship import ContentRelationship
from app.models.source import Source
from app.services.relationship_service import (
    DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION,
    MAX_CANDIDATES_PER_CONTENT,
    MAX_RELATIONSHIPS_PER_CONTENT,
    RelationshipService,
    jaccard_similarity,
    normalize_token_set,
)
from app.db import Base
from test_main import TestingSessionLocal, client, test_engine


def setup_function(_: object) -> None:
    """Reset the shared in-memory database before each relationship test."""

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def _content(database, source_id, fingerprint: str, *, title: str, topics: list[str], keywords: list[str], category: str = "automacao", published_at: datetime | None = None) -> Content:
    """Persist a processed item eligible for deterministic relationship rebuilding."""

    item = Content(source_id=source_id, title=title, url=f"https://test/{fingerprint}", fingerprint=fingerprint, topics=topics, keywords=keywords, category=category, summary="automation workflow", processing_status="processed", published_at=published_at)
    database.add(item)
    database.commit()
    database.refresh(item)
    return item


def _source(database) -> Source:
    """Persist a source for relationship test records."""

    source = Source(name=f"relationship-source-{datetime.now(UTC).timestamp()}", type="manual")
    database.add(source)
    database.commit()
    database.refresh(source)
    return source


def test_normalization_and_jaccard_are_deterministic() -> None:
    """Comparison normalization is accent/case insensitive and stable."""

    assert normalize_token_set([" ChatGPT ", "chatgpt", "Automação", None, ""]) == ["automacao", "chatgpt"]
    assert jaccard_similarity({"automacao", "chatgpt"}, {"automacao", "openai"}) == 1 / 3
    assert jaccard_similarity(set(), set()) == 0


def test_rebuild_persists_canonical_explainable_pair_and_is_idempotent() -> None:
    """A rebuild creates one canonical pair and a repeated rebuild performs no write."""

    database = TestingSessionLocal()
    source = _source(database)
    today = datetime.now(UTC)
    first = _content(database, source.id, "relationship-first", title="ChatGPT automation", topics=["ChatGPT", "automação"], keywords=["openai", "workflow"], published_at=today)
    second = _content(database, source.id, "relationship-second", title="ChatGPT workflow", topics=["chatgpt", "automacao"], keywords=["openai", "workflow"], published_at=today - timedelta(days=2))
    first_run = client.post(f"/relationships/contents/{first.id}/rebuild")
    second_run = client.post(f"/relationships/contents/{first.id}/rebuild")
    relationship = database.scalar(select(ContentRelationship))

    assert first_run.status_code == 200
    assert first_run.json()["relationships_created"] == 1
    assert second_run.json()["relationships_unchanged"] == 1
    assert relationship is not None
    assert str(relationship.content_id) < str(relationship.related_content_id)
    assert relationship.algorithm_version == DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION
    assert float(relationship.score) >= 20
    assert relationship.score_breakdown["topics"] == 35.0
    assert relationship.shared_topics == ["automacao", "chatgpt"]
    database.close()


def test_dry_run_and_stale_removal_do_not_leave_obsolete_pairs() -> None:
    """Dry runs are write-free, while real rebuilds remove pairs no longer qualified."""

    database = TestingSessionLocal()
    source = _source(database)
    first = _content(database, source.id, "dry-first", title="same", topics=["automation"], keywords=["openai"], published_at=datetime.now(UTC))
    second = _content(database, source.id, "dry-second", title="same", topics=["automation"], keywords=["openai"], published_at=datetime.now(UTC))
    dry = client.post(f"/relationships/contents/{first.id}/rebuild?dry_run=true")
    assert dry.status_code == 200 and dry.json()["relationships_created"] == 1
    assert database.scalar(select(ContentRelationship)) is None
    assert client.post(f"/relationships/contents/{first.id}/rebuild").status_code == 200
    second.topics, second.keywords, second.category, second.title, second.summary = [], [], "other", "unrelated", ""
    database.commit()
    removed = client.post(f"/relationships/contents/{first.id}/rebuild")
    assert removed.json()["relationships_deleted"] == 1
    assert database.scalar(select(ContentRelationship)) is None
    database.close()


def test_related_recommendations_and_between_work_from_either_pair_side() -> None:
    """Read endpoints expose the other content side and canonicalize lookup order."""

    database = TestingSessionLocal()
    source = _source(database)
    first = _content(database, source.id, "read-first", title="workflow", topics=["automation"], keywords=["openai"])
    second = _content(database, source.id, "read-second", title="workflow", topics=["automation"], keywords=["openai"])
    assert client.post(f"/relationships/contents/{first.id}/rebuild").status_code == 200
    related = client.get(f"/contents/{second.id}/related?page=1&page_size=20")
    recommendations = client.get(f"/contents/{first.id}/recommendations")
    between = client.get(f"/relationships/between/{second.id}/{first.id}")

    assert related.status_code == recommendations.status_code == between.status_code == 200
    assert related.json()["total"] == 1 and related.json()["items"][0]["content"]["id"] == str(first.id)
    assert recommendations.json()["total"] == 1
    assert between.json()["relationship"]["content_id"] != between.json()["relationship"]["related_content_id"]
    database.close()


def test_batch_rebuild_is_bounded_and_openapi_documents_relationship_routes() -> None:
    """The batch contract is bounded and all Sprint 06 API paths are advertised."""

    database = TestingSessionLocal()
    source = _source(database)
    first = _content(database, source.id, "batch-first", title="workflow", topics=["automation"], keywords=["openai"])
    _content(database, source.id, "batch-second", title="workflow", topics=["automation"], keywords=["openai"])
    database.close()
    response = client.post("/relationships/rebuild", json={"limit": 1, "dry_run": True})
    invalid = client.post("/relationships/rebuild", json={"limit": 501})
    paths = client.get("/openapi.json").json()["paths"]

    assert response.status_code == 200 and response.json()["contents_processed"] == 1
    assert invalid.status_code == 422
    assert {f"/relationships/contents/{{content_id}}/rebuild", "/relationships/rebuild", "/contents/{content_id}/related", "/contents/{content_id}/recommendations", "/relationships/between/{content_id}/{related_content_id}"}.issubset(paths)


def test_database_constraints_block_self_duplicate_inverse_and_invalid_scores() -> None:
    """The database itself protects canonical pairs and the 0..100 score range."""

    database = TestingSessionLocal()
    source = _source(database)
    first = _content(database, source.id, "constraint-first", title="same", topics=["automation"], keywords=[])
    second = _content(database, source.id, "constraint-second", title="same", topics=["automation"], keywords=[])
    third = _content(database, source.id, "constraint-third", title="same", topics=["automation"], keywords=[])
    low, high = sorted((first.id, second.id), key=str)
    database.add(_relationship(low, high, score=20))
    database.commit()
    for invalid in (
        _relationship(low, low, score=20),
        _relationship(low, high, score=20),
        _relationship(high, low, score=20),
        _relationship(*sorted((low, third.id), key=str), score=101),
    ):
        database.add(invalid)
        try:
            database.commit()
            assert False, "constraint should reject the invalid relationship"
        except IntegrityError:
            database.rollback()
    database.close()


def test_dry_run_update_and_delete_preserve_persisted_row() -> None:
    """Dry run reports recalculation/removal without writing fields or deleting the pair."""

    database = TestingSessionLocal()
    source = _source(database)
    first = _content(database, source.id, "dry-change-first", title="ChatGPT workflow", topics=["automation"], keywords=["openai"])
    second = _content(database, source.id, "dry-change-second", title="ChatGPT workflow", topics=["automation"], keywords=["openai"])
    client.post(f"/relationships/contents/{first.id}/rebuild")
    relationship = database.scalar(select(ContentRelationship))
    assert relationship is not None
    original_updated_at, original_score = relationship.updated_at, relationship.score
    second.title = "different document text"
    database.commit()
    updating = client.post(f"/relationships/contents/{first.id}/rebuild?dry_run=true")
    database.expire_all()
    unchanged = database.scalar(select(ContentRelationship))
    assert updating.json()["relationships_updated"] == 1
    assert unchanged is not None and unchanged.updated_at == original_updated_at and unchanged.score == original_score
    second.title, second.summary, second.topics, second.keywords, second.category = "unrelated", "", [], [], "other"
    database.commit()
    deleting = client.post(f"/relationships/contents/{first.id}/rebuild?dry_run=true")
    assert deleting.json()["relationships_deleted"] == 1
    assert database.scalar(select(ContentRelationship)) is not None
    database.close()


def test_candidate_and_top_relationship_limits_are_enforced() -> None:
    """Candidate retrieval is capped before scoring and persistence keeps only top 50."""

    database = TestingSessionLocal()
    source = _source(database)
    target = _content(database, source.id, "limit-target", title="workflow", topics=[], keywords=[], category="automacao")
    target_id = target.id
    candidates = [
        Content(source_id=source.id, title=f"workflow {index}", url=f"https://test/limit-{index}", fingerprint=f"limit-{index}", category="automacao", topics=[], keywords=[], processing_status="processed")
        for index in range(MAX_CANDIDATES_PER_CONTENT + 1)
    ]
    database.add_all(candidates)
    database.commit()
    database.close()
    result = client.post(f"/relationships/contents/{target_id}/rebuild").json()
    database = TestingSessionLocal()
    count = len(list(database.scalars(select(ContentRelationship).where(or_(ContentRelationship.content_id == target_id, ContentRelationship.related_content_id == target_id)))))
    assert result["candidates_evaluated"] == MAX_CANDIDATES_PER_CONTENT
    assert result["relationships_created"] == MAX_RELATIONSHIPS_PER_CONTENT
    assert count == MAX_RELATIONSHIPS_PER_CONTENT
    database.close()


def test_batch_rebuild_reports_individual_controlled_errors(monkeypatch: object) -> None:
    """A single rebuild failure is aggregated without a stack trace escaping the API."""

    database = TestingSessionLocal()
    source = _source(database)
    _content(database, source.id, "batch-error", title="workflow", topics=["automation"], keywords=[])
    database.close()

    def fail_once(self: RelationshipService, *_: object, **__: object):
        raise ValueError("controlled rebuild failure")

    monkeypatch.setattr(RelationshipService, "rebuild_content", fail_once)
    result = client.post("/relationships/rebuild", json={"limit": 1}).json()
    assert result["contents_failed"] == 1
    assert result["errors"][0]["error"] == "controlled rebuild failure"


def test_related_supports_pagination_filters_and_same_source_exclusion() -> None:
    """Related results paginate and apply filters to the returned opposite content."""

    database = TestingSessionLocal()
    first_source = _source(database)
    second_source = _source(database)
    target = _content(database, first_source.id, "filter-target", title="workflow", topics=["automation"], keywords=[])
    target_id = target.id
    second_source_id = second_source.id
    _content(database, first_source.id, "filter-same-source", title="workflow", topics=["automation"], keywords=[], category="automacao")
    _content(database, second_source.id, "filter-other-source", title="workflow", topics=["automation"], keywords=[], category="tecnologia")
    database.close()
    client.post(f"/relationships/contents/{target_id}/rebuild")
    paged = client.get(f"/contents/{target_id}/related?page=1&page_size=1")
    category = client.get(f"/contents/{target_id}/related?category=tecnologia")
    excluded = client.get(f"/contents/{target_id}/related?exclude_same_source=true")

    assert paged.json()["total"] == 2 and len(paged.json()["items"]) == 1
    assert category.json()["total"] == 1 and category.json()["items"][0]["content"]["category"] == "tecnologia"
    assert excluded.json()["total"] == 1 and excluded.json()["items"][0]["content"]["source_id"] == str(second_source_id)
    assert client.get(f"/contents/{target_id}/related?page=0").status_code == 422
    assert client.post("/relationships/rebuild", json={"published_after": "2026-02-01T00:00:00Z", "published_before": "2026-01-01T00:00:00Z"}).status_code == 422


def test_foreign_key_cascade_removes_relationship_when_either_content_is_deleted() -> None:
    """Both content foreign keys use ON DELETE CASCADE in the persistence model."""

    database = TestingSessionLocal()
    database.execute(text("PRAGMA foreign_keys = ON"))
    source = _source(database)
    first = _content(database, source.id, "cascade-first", title="same", topics=[], keywords=[])
    second = _content(database, source.id, "cascade-second", title="same", topics=[], keywords=[])
    low, high = sorted((first.id, second.id), key=str)
    database.add(_relationship(low, high, score=20))
    database.commit()
    database.delete(second)
    database.commit()
    assert database.scalar(select(ContentRelationship)) is None
    database.close()


def _relationship(first_id, second_id, *, score: int) -> ContentRelationship:
    """Construct a minimal valid relationship for database-constraint tests."""

    return ContentRelationship(
        content_id=first_id, related_content_id=second_id, score=score,
        score_breakdown={"topics": 0, "keywords": 0, "category": 0, "text": 0, "temporal": 0, "source": 0},
        shared_topics=[], shared_keywords=[], same_category=False, same_source=False,
        text_similarity=0, published_distance_days=None, reasons=[],
        algorithm_version=DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION,
    )

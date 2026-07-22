"""Real PostgreSQL FTS tests for the isolated lexical candidate repository."""

from uuid import UUID, uuid4

from sqlalchemy import text

from app.db import SessionLocal
from app.models.content import Content
from app.models.source import Source
from app.repositories.lexical_search_repository import LexicalSearchRepository


def _content(source_id: UUID, marker: str, title: str, **values: object) -> Content:
    """Create a content row whose trigger-maintained FTS data is tested in one transaction."""

    defaults: dict[str, object] = {
        "topics": [],
        "keywords": [],
        "processing_status": "processed",
    }
    defaults.update(values)
    return Content(
        source_id=source_id,
        title=title,
        url=f"https://test/fts/{marker}",
        fingerprint=f"fts-{marker}",
        **defaults,
    )


def test_lexical_repository_uses_persisted_fts_weights_and_deterministic_order() -> None:
    """Trigger-backed FTS covers every weighted field, language text and stable ties."""

    database = SessionLocal()
    marker = uuid4().hex
    try:
        source = Source(name=f"sprint09-fts-{marker}", type="manual")
        database.add(source)
        database.flush()
        title = _content(source.id, f"{marker}-title", "alpha-title-token")
        keywords = _content(source.id, f"{marker}-keywords", "keywords", keywords=["alpha-keyword-token"])
        topics = _content(source.id, f"{marker}-topics", "topics", topics=["alpha-topic-token"])
        summary = _content(source.id, f"{marker}-summary", "summary", summary="alpha-summary-token")
        category = _content(source.id, f"{marker}-category", "category", category="alpha-category-token")
        accent = _content(source.id, f"{marker}-accent", "Automação avançada", summary=None, category=None)
        english = _content(source.id, f"{marker}-english", "Workflow automation", summary=None, category=None)
        nulls = _content(source.id, f"{marker}-nulls", "nullable fields", summary=None, category=None, topics=[], keywords=[])
        weighted_title = _content(source.id, f"{marker}-weighted-title", "weight-token")
        weighted_summary = _content(source.id, f"{marker}-weighted-summary", "other", summary="weight-token")
        tie_first = _content(source.id, f"{marker}-tie-a", "tie-token", id=UUID("00000000-0000-0000-0000-000000000001"))
        tie_second = _content(source.id, f"{marker}-tie-b", "tie-token", id=UUID("00000000-0000-0000-0000-000000000002"))
        database.add_all([title, keywords, topics, summary, category, accent, english, nulls, weighted_title, weighted_summary, tie_first, tie_second])
        database.flush()
        repository = LexicalSearchRepository(database)
        title_candidates = repository.search("alpha-title-token", 10)
        assert [candidate.content_id for candidate in title_candidates] == [title.id]
        assert repository.search("alpha-keyword-token", 10)[0].content_id == keywords.id
        assert repository.search("alpha-topic-token", 10)[0].content_id == topics.id
        assert repository.search("alpha-summary-token", 10)[0].content_id == summary.id
        assert repository.search("alpha-category-token", 10)[0].content_id == category.id
        assert repository.search("AUTOMACAO", 10)[0].content_id == accent.id
        assert repository.search("workflow", 10)[0].content_id == english.id
        weighted = repository.search("weight-token", 10)
        assert [candidate.content_id for candidate in weighted[:2]] == [weighted_title.id, weighted_summary.id]
        tied = repository.search("tie-token", 10)
        assert [candidate.content_id for candidate in tied] == [tie_first.id, tie_second.id]
        assert repository.search("tie-token", 1) == [tied[0]]
        assert repository.search("absent-token", 10) == []
        assert repository.search("   ", 10) == []
        assert repository.search("!!!@@@", 10) == []
        try:
            repository.search("valid", 0)
        except ValueError as error:
            assert str(error) == "limit must be greater than zero"
        else:
            raise AssertionError("non-positive limit must fail")
        before = database.execute(text("SELECT count(*) FROM contents WHERE source_id = :source_id"), {"source_id": source.id}).scalar_one()
        repository.search("alpha-title-token", 10)
        after = database.execute(text("SELECT count(*) FROM contents WHERE source_id = :source_id"), {"source_id": source.id}).scalar_one()
        assert before == after == 12
        gin_indexes = database.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'contents' AND indexname = 'ix_contents_search_vector_gin'"))
        assert gin_indexes.scalar_one() == "ix_contents_search_vector_gin"
        search_vector = database.execute(text("SELECT search_vector IS NOT NULL FROM contents WHERE id = :content_id"), {"content_id": title.id}).scalar_one()
        assert search_vector is True
    finally:
        database.rollback()
        database.close()
    verification = SessionLocal()
    try:
        assert verification.query(Source).filter(Source.name == f"sprint09-fts-{marker}").count() == 0
    finally:
        verification.close()

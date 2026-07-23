"""Unit and PostgreSQL integration tests for partial reranking document hydration."""

from dataclasses import fields
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models.content import Content
from app.models.source import Source
from app.repositories.rerank_document_repository import RerankDocumentRecord, RerankDocumentRepository


class FakeDatabase:
    """Minimal query spy returning rows in deliberately arbitrary database order."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.calls = 0

    def execute(self, _statement):
        self.calls += 1
        return self.rows


def _row(content_id: UUID, title: str = "Title", topics: object = None, keywords: object = None) -> tuple[object, ...]:
    return (content_id, title, None, None, topics, keywords)


def test_empty_input_skips_query_and_valid_generator_is_materialized_once() -> None:
    database = FakeDatabase([])
    repository = RerankDocumentRepository(database)  # type: ignore[arg-type]
    assert repository.hydrate([]) == [] and database.calls == 0

    first, second = uuid4(), uuid4()
    database.rows = [_row(second, "Second", ["second"], []), _row(first, "First", [], ["first"])]
    ids = (content_id for content_id in [first, second])
    result = repository.hydrate(ids)
    assert [record.content_id for record in result] == [first, second]
    assert database.calls == 1


def test_hydration_preserves_input_order_omits_missing_and_maps_partial_fields() -> None:
    first, missing, last = uuid4(), uuid4(), uuid4()
    database = FakeDatabase([
        _row(last, "Last", ["second", "first"], ["beta", "alpha"]),
        _row(first, "First", None, None),
    ])
    result = RerankDocumentRepository(database).hydrate([first, missing, last])  # type: ignore[arg-type]
    assert result == [
        RerankDocumentRecord(first, "First", None, None, (), ()),
        RerankDocumentRecord(last, "Last", None, None, ("second", "first"), ("beta", "alpha")),
    ]
    assert database.calls == 1


def test_preserves_nullable_text_fields_and_empty_term_collections() -> None:
    content_id = uuid4()
    database = FakeDatabase([(content_id, None, None, None, [], [])])
    assert RerankDocumentRepository(database).hydrate([content_id]) == [  # type: ignore[arg-type]
        RerankDocumentRecord(content_id, None, None, None, (), ())
    ]


@pytest.mark.parametrize(
    "content_ids",
    [None, 1, "not-ids", b"not-ids", [None], ["uuid"], [object()], [uuid4(), uuid4()] * 2],
)
def test_invalid_ids_fail_before_query(content_ids: object) -> None:
    database = FakeDatabase([])
    with pytest.raises(ValueError):
        RerankDocumentRepository(database).hydrate(content_ids)  # type: ignore[arg-type]
    assert database.calls == 0


@pytest.mark.parametrize("value", [{"term": "value"}, ["valid", 1], "term"])
def test_rejects_incompatible_persisted_term_shapes_without_partial_result(value: object) -> None:
    content_id = uuid4()
    database = FakeDatabase([_row(content_id, topics=value)])
    with pytest.raises(ValueError):
        RerankDocumentRepository(database).hydrate([content_id])  # type: ignore[arg-type]
    assert database.calls == 1


def test_propagates_sqlalchemy_errors_without_fallback() -> None:
    class FailingDatabase:
        def execute(self, _statement):
            raise OperationalError("select", {}, Exception("offline"))

    with pytest.raises(OperationalError):
        RerankDocumentRepository(FailingDatabase()).hydrate([uuid4()])  # type: ignore[arg-type]


def test_record_is_frozen_slotted_and_excludes_forbidden_fields() -> None:
    record = RerankDocumentRecord(uuid4(), None, None, None, (), ())
    with pytest.raises((AttributeError, TypeError)):
        record.title = "changed"  # type: ignore[misc]
    names = {field.name for field in fields(RerankDocumentRecord)}
    assert names == {"content_id", "title", "summary", "category", "topics", "keywords"}
    assert not names & {"url", "raw_payload", "embedding", "processing_status", "source_id", "score", "rank", "matched_by"}


def _content(source_id: UUID, marker: str, title: str, status: str, topics: list[str], keywords: list[str]) -> Content:
    return Content(
        source_id=source_id,
        title=title,
        url=f"https://rerank.test/{marker}",
        fingerprint=f"rerank-document-{marker}",
        summary=None,
        category=None,
        topics=topics,
        keywords=keywords,
        processing_status=status,
    )


def test_postgresql_hydrates_only_partial_fields_once_preserves_order_and_rolls_back() -> None:
    database = SessionLocal()
    marker = uuid4().hex
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    try:
        source = Source(name=f"sprint11-rerank-{marker}", type="manual")
        database.add(source)
        database.flush()
        processed = _content(source.id, f"{marker}-processed", "Processed", "processed", ["Topic B", "Topic A"], ["Key B", "Key A"])
        pending = _content(source.id, f"{marker}-pending", "Pending", "pending", [], [])
        database.add_all([processed, pending])
        database.flush()
        missing = uuid4()
        event.listen(database.bind, "before_cursor_execute", listener)
        records = RerankDocumentRepository(database).hydrate([pending.id, missing, processed.id])
        event.remove(database.bind, "before_cursor_execute", listener)
        assert [record.content_id for record in records] == [pending.id, processed.id]
        assert records[0].title == "Pending" and records[0].topics == records[0].keywords == ()
        assert records[1].topics == ("Topic B", "Topic A") and records[1].keywords == ("Key B", "Key A")
        assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
        selected_sql = statements[0].lower()
        assert "raw_payload" not in selected_sql and "processing_status" not in selected_sql and " url" not in selected_sql
    finally:
        if event.contains(database.bind, "before_cursor_execute", listener):
            event.remove(database.bind, "before_cursor_execute", listener)
        database.rollback()
        database.close()

    verification = SessionLocal()
    try:
        assert verification.scalar(select(Source).where(Source.name == f"sprint11-rerank-{marker}")) is None
    finally:
        verification.close()

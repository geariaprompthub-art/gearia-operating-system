"""Unit and PostgreSQL integration tests for ordered content eligibility filtering."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.db import Base, SessionLocal
from app.models.content import Content
from app.models.source import Source
from app.repositories.content_eligibility_repository import ContentEligibilityRepository
from test_main import TestingSessionLocal, test_engine


def setup_function(_: object) -> None:
    """Reset the shared SQLite test schema before each repository unit test."""

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def _content(database, source_id: UUID, content_id: UUID, marker: str, status: str) -> Content:
    """Create a minimal content record with the requested processing status."""

    content = Content(
        id=content_id,
        source_id=source_id,
        title=marker,
        url=f"https://test/eligibility/{marker}",
        fingerprint=f"eligibility-{marker}",
        processing_status=status,
    )
    database.add(content)
    return content


def _unit_fixture(database) -> tuple[UUID, UUID, UUID]:
    """Persist processed and non-processed records with stable input identifiers."""

    source = Source(name=f"eligibility-{uuid4().hex}", type="manual")
    database.add(source)
    database.flush()
    eligible_first, ineligible, eligible_last = uuid4(), uuid4(), uuid4()
    _content(database, source.id, eligible_first, "eligible-first", "processed")
    _content(database, source.id, ineligible, "ineligible", "pending")
    _content(database, source.id, eligible_last, "eligible-last", "processed")
    database.commit()
    return eligible_first, ineligible, eligible_last


def test_filter_eligible_preserves_input_order_and_omits_missing_or_not_processed() -> None:
    """One result list reflects caller order rather than incidental database ordering."""

    database = TestingSessionLocal()
    try:
        first, ineligible, last = _unit_fixture(database)
        missing = uuid4()
        repository = ContentEligibilityRepository(database)
        input_ids = [last, missing, ineligible, first]
        original = list(input_ids)
        assert repository.filter_eligible(input_ids) == [last, first]
        assert input_ids == original
        assert repository.filter_eligible([ineligible, missing]) == []
        assert repository.filter_eligible([first, last]) == [first, last]
    finally:
        database.close()


def test_filter_eligible_empty_input_skips_query() -> None:
    """No empty IN query is issued for an empty collection."""

    database = TestingSessionLocal()
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(database.bind, "before_cursor_execute", listener)
    try:
        assert ContentEligibilityRepository(database).filter_eligible([]) == []
        assert statements == []
    finally:
        event.remove(database.bind, "before_cursor_execute", listener)
        database.close()


@pytest.mark.parametrize(
    "content_ids",
    [None, 42, "not-an-id-list", b"not-an-id-list", ["not-a-uuid"], [UUID("00000000-0000-0000-0000-000000000001")] * 2],
)
def test_filter_eligible_rejects_invalid_input_before_query(content_ids: object) -> None:
    """Validation is complete and side-effect-free before query execution."""

    database = TestingSessionLocal()
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(database.bind, "before_cursor_execute", listener)
    try:
        with pytest.raises(ValueError):
            ContentEligibilityRepository(database).filter_eligible(content_ids)  # type: ignore[arg-type]
        assert statements == []
    finally:
        event.remove(database.bind, "before_cursor_execute", listener)
        database.close()


def test_filter_eligible_propagates_database_failures() -> None:
    """Dependency failures remain visible to the future service boundary."""

    class FailingDatabase:
        def scalars(self, _statement):
            raise OperationalError("select", {}, Exception("database unavailable"))

    with pytest.raises(OperationalError):
        ContentEligibilityRepository(FailingDatabase()).filter_eligible([uuid4()])  # type: ignore[arg-type]


def test_content_eligibility_repository_postgresql_filters_once_and_rolls_back() -> None:
    """Real PostgreSQL applies the status predicate in one SELECT without residue."""

    database = SessionLocal()
    marker = uuid4().hex
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    try:
        source = Source(name=f"sprint10-eligibility-{marker}", type="manual")
        database.add(source)
        database.flush()
        first, pending, last = uuid4(), uuid4(), uuid4()
        _content(database, source.id, first, f"{marker}-first", "processed")
        _content(database, source.id, pending, f"{marker}-pending", "pending")
        _content(database, source.id, last, f"{marker}-last", "processed")
        database.flush()
        missing = uuid4()
        event.listen(database.bind, "before_cursor_execute", listener)
        result = ContentEligibilityRepository(database).filter_eligible([last, missing, pending, first])
        event.remove(database.bind, "before_cursor_execute", listener)
        assert result == [last, first]
        assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    finally:
        if event.contains(database.bind, "before_cursor_execute", listener):
            event.remove(database.bind, "before_cursor_execute", listener)
        database.rollback()
        database.close()

    verification = SessionLocal()
    try:
        assert verification.scalar(select(Source).where(Source.name == f"sprint10-eligibility-{marker}")) is None
    finally:
        verification.close()

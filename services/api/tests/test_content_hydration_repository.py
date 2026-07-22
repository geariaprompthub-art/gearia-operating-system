"""PostgreSQL integration tests for ordered, side-effect-free content hydration."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from app.db import SessionLocal
from app.models.content import Content
from app.models.source import Source
from app.repositories.content_hydration_repository import ContentHydrationRepository


def _content(source_id: UUID, marker: str, title: str, summary: str | None) -> Content:
    return Content(
        source_id=source_id,
        title=title,
        url=f"https://test/hydration/{marker}",
        fingerprint=f"hydration-{marker}",
        summary=summary,
        processing_status="processed",
    )


def test_content_hydration_preserves_order_uses_one_query_and_leaves_no_residue() -> None:
    """One SELECT hydrates known IDs, omits missing ones and never writes state."""

    database = SessionLocal()
    marker = uuid4().hex
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    try:
        source = Source(name=f"sprint09-hydration-{marker}", type="manual")
        database.add(source)
        database.flush()
        first = _content(source.id, f"{marker}-one", "First", "first summary")
        second = _content(source.id, f"{marker}-two", "Second", None)
        third = _content(source.id, f"{marker}-three", "Third", "third summary")
        database.add_all([first, second, third])
        database.flush()
        repository = ContentHydrationRepository(database)
        missing = uuid4()
        before = database.execute(text("SELECT count(*) FROM contents WHERE source_id = :source_id"), {"source_id": source.id}).scalar_one()
        event.listen(database.bind, "before_cursor_execute", listener)
        hydrated = repository.hydrate([third.id, missing, first.id, second.id])
        event.remove(database.bind, "before_cursor_execute", listener)
        assert [item.content_id for item in hydrated] == [third.id, first.id, second.id]
        assert [item.title for item in hydrated] == ["Third", "First", "Second"]
        assert hydrated[-1].summary is None
        assert len(statements) == 1 and "SELECT" in statements[0]
        assert repository.hydrate([first.id])[0].content_id == first.id
        assert hydrated == repository.hydrate([third.id, missing, first.id, second.id])
        assert repository.hydrate([missing]) == []
        statements.clear()
        event.listen(database.bind, "before_cursor_execute", listener)
        assert repository.hydrate([]) == []
        event.remove(database.bind, "before_cursor_execute", listener)
        assert statements == []
        after = database.execute(text("SELECT count(*) FROM contents WHERE source_id = :source_id"), {"source_id": source.id}).scalar_one()
        assert before == after == 3
    finally:
        if event.contains(database.bind, "before_cursor_execute", listener):
            event.remove(database.bind, "before_cursor_execute", listener)
        database.rollback()
        database.close()
    verification = SessionLocal()
    try:
        assert verification.query(Source).filter(Source.name == f"sprint09-hydration-{marker}").count() == 0
    finally:
        verification.close()


@pytest.mark.parametrize("content_ids", [None, ["not-a-uuid"], [UUID("00000000-0000-0000-0000-000000000001")] * 2])
def test_content_hydration_rejects_invalid_input_before_query(content_ids: object) -> None:
    """Invalid input cannot produce a partial hydration query."""

    database = SessionLocal()
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(database.bind, "before_cursor_execute", listener)
    try:
        with pytest.raises(ValueError):
            ContentHydrationRepository(database).hydrate(content_ids)  # type: ignore[arg-type]
        assert statements == []
    finally:
        event.remove(database.bind, "before_cursor_execute", listener)
        database.rollback()
        database.close()

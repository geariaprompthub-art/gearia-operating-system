"""Tests for read-only projection of canonical content relationships."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text

from app.db import Base, SessionLocal
from app.models.content import Content
from app.models.content_relationship import ContentRelationship
from app.models.source import Source
from app.repositories.content_relationship_repository import ContentRelationshipRepository
from test_main import TestingSessionLocal, test_engine


def setup_function(_: object) -> None:
    """Isolate SQLite unit tests from the shared test database."""

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def _content(database, source_id: UUID, content_id: UUID, marker: str) -> Content:
    """Create a minimal processed content fixture with a stable UUID."""

    item = Content(
        id=content_id,
        source_id=source_id,
        title=marker,
        url=f"https://test/relationship-repository/{marker}",
        fingerprint=f"relationship-repository-{marker}",
        processing_status="processed",
    )
    database.add(item)
    return item


def _relationship(first_id: UUID, second_id: UUID, *, score: Decimal, version: str = "deterministic-v1") -> ContentRelationship:
    """Build a valid canonical relationship fixture."""

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
        algorithm_version=version,
    )


def _unit_fixtures(database):
    """Create canonical pairs that exercise both physical sides of the table."""

    source = Source(name=f"relationship-repository-{uuid4().hex}", type="manual")
    database.add(source)
    database.flush()
    low, middle, high, top, lonely = sorted((uuid4(), uuid4(), uuid4(), uuid4(), uuid4()), key=str)
    for content_id, marker in zip((low, middle, high, top, lonely), ("low", "middle", "high", "top", "lonely"), strict=True):
        _content(database, source.id, content_id, marker)
    database.add_all(
        [
            _relationship(low, middle, score=Decimal("80.00"), version="deterministic-v1"),
            _relationship(low, top, score=Decimal("20.00"), version="deterministic-v1"),
            _relationship(high, top, score=Decimal("70.00"), version="deterministic-v2"),
        ]
    )
    database.commit()
    return low, middle, high, top, lonely


def test_neighbors_projects_both_sides_preserves_metadata_and_is_deterministic() -> None:
    """Canonical storage is exposed as sorted logical adjacency without aggregation."""

    database = TestingSessionLocal()
    try:
        low, middle, high, top, lonely = _unit_fixtures(database)
        repository = ContentRelationshipRepository(database)

        low_neighbors = repository.neighbors([low])
        assert [(item.seed_content_id, item.neighbor_content_id, item.edge_score) for item in low_neighbors] == [
            (low, middle, Decimal("80.00")),
            (low, top, Decimal("20.00")),
        ]
        assert [item.algorithm_version for item in low_neighbors] == ["deterministic-v1", "deterministic-v1"]

        top_neighbors = repository.neighbors([top])
        assert [(item.seed_content_id, item.neighbor_content_id, item.edge_score) for item in top_neighbors] == [
            (top, high, Decimal("70.00")),
            (top, low, Decimal("20.00")),
        ]
        assert [item.algorithm_version for item in top_neighbors] == ["deterministic-v2", "deterministic-v1"]
        assert repository.neighbors([lonely]) == []

        combined = repository.neighbors([top, low])
        assert [(item.seed_content_id, item.neighbor_content_id) for item in combined] == [
            (low, middle),
            (low, top),
            (top, high),
            (top, low),
        ]
        assert combined == repository.neighbors([low, top])
    finally:
        database.close()


@pytest.mark.parametrize("seed_ids", [None, ["not-a-uuid"], [UUID("00000000-0000-0000-0000-000000000001")] * 2, 42])
def test_neighbors_rejects_invalid_input_before_query(seed_ids: object) -> None:
    """Invalid seeds cannot trigger a partial relationship lookup."""

    database = TestingSessionLocal()
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(database.bind, "before_cursor_execute", listener)
    try:
        with pytest.raises(ValueError):
            ContentRelationshipRepository(database).neighbors(seed_ids)  # type: ignore[arg-type]
        assert statements == []
    finally:
        event.remove(database.bind, "before_cursor_execute", listener)
        database.close()


def test_neighbors_empty_input_skips_query_and_defensively_omits_self_loop() -> None:
    """Empty input and an impossible corrupt self-loop never produce adjacency."""

    class FakeDatabase:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _statement):
            self.calls += 1
            return [(uuid4(), seed, seed, Decimal("50.00"), "deterministic-v1")]

    seed = uuid4()
    database = FakeDatabase()
    repository = ContentRelationshipRepository(database)  # type: ignore[arg-type]
    assert repository.neighbors([]) == []
    assert database.calls == 0
    assert repository.neighbors([seed]) == []
    assert database.calls == 1


def test_relationship_repository_postgresql_reads_both_sides_once_and_rolls_back() -> None:
    """Real PostgreSQL reads both pair sides in one SELECT and leaves no residue."""

    database = SessionLocal()
    marker = uuid4().hex
    statements: list[str] = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    try:
        source = Source(name=f"sprint10-relationship-repository-{marker}", type="manual")
        database.add(source)
        database.flush()
        low, middle, high = sorted((uuid4(), uuid4(), uuid4()), key=str)
        _content(database, source.id, low, f"{marker}-low")
        _content(database, source.id, middle, f"{marker}-middle")
        _content(database, source.id, high, f"{marker}-high")
        database.flush()
        database.add_all([
            _relationship(low, middle, score=Decimal("90.00")),
            _relationship(middle, high, score=Decimal("40.00")),
        ])
        database.flush()

        repository = ContentRelationshipRepository(database)
        event.listen(database.bind, "before_cursor_execute", listener)
        neighbors = repository.neighbors([middle])
        event.remove(database.bind, "before_cursor_execute", listener)

        assert [(item.seed_content_id, item.neighbor_content_id, item.edge_score) for item in neighbors] == [
            (middle, low, Decimal("90.00")),
            (middle, high, Decimal("40.00")),
        ]
        assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
        index_names = set(database.scalars(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'content_relationships'")))
        assert {"ix_content_relationships_content_id", "ix_content_relationships_related_content_id"}.issubset(index_names)
    finally:
        if event.contains(database.bind, "before_cursor_execute", listener):
            event.remove(database.bind, "before_cursor_execute", listener)
        database.rollback()
        database.close()

    verification = SessionLocal()
    try:
        assert verification.scalar(select(Source).where(Source.name == f"sprint10-relationship-repository-{marker}")) is None
    finally:
        verification.close()

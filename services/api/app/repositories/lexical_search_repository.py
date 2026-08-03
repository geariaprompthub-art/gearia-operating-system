"""PostgreSQL full-text candidate retrieval for future hybrid composition."""

from dataclasses import dataclass
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.content import Content


@dataclass(frozen=True)
class LexicalSearchCandidate:
    """Minimum lexical candidate contract; rank remains private to the repository."""

    content_id: UUID


class LexicalSearchRepository:
    """Read ordered FTS candidates from the persisted PostgreSQL search vector."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def search(
        self, query: str, limit: int, visible_content_ids: Sequence[UUID] | None = None
    ) -> list[LexicalSearchCandidate]:
        """Return deterministic candidates without hydration or persistence."""

        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        normalized_query = query.strip()
        if not normalized_query:
            return []
        tsquery = func.plainto_tsquery("simple", func.unaccent(normalized_query))
        if (self._database.scalar(select(func.numnode(tsquery))) or 0) == 0:
            return []
        rank = func.ts_rank_cd(Content.search_vector, tsquery)
        statement = (
            select(Content.id)
            .where(Content.search_vector.op("@@")(tsquery))
            .order_by(rank.desc(), Content.id.asc())
            .limit(limit)
        )
        if visible_content_ids is not None:
            if not visible_content_ids:
                return []
            statement = statement.where(Content.id.in_(visible_content_ids))
        return [LexicalSearchCandidate(content_id=content_id) for content_id in self._database.scalars(statement)]

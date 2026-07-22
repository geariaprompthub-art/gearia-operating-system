"""Database-backed PostgreSQL full-text search service."""

from datetime import datetime
from math import ceil
from uuid import UUID

from sqlalchemy import Float, String, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.content import Content
from app.schemas.search import SearchResponse, SearchResultItem, SearchSortBy, SortOrder


class SearchService:
    """Search indexed contents; the PostgreSQL branch uses FTS and ts_rank_cd."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def search(
        self,
        *,
        query: str | None,
        source_id: UUID | None,
        category: str | None,
        topic: str | None,
        language: str | None,
        processing_status: str | None,
        min_relevance_score: int | None,
        max_relevance_score: int | None,
        published_from: datetime | None,
        published_to: datetime | None,
        page: int,
        page_size: int,
        sort_by: SearchSortBy | None,
        sort_order: SortOrder,
    ) -> SearchResponse:
        """Run search, filters, counting and pagination inside the database."""

        normalized_query = query.strip() if query and query.strip() else None
        conditions = self._filters(
            source_id, category, topic, language, processing_status,
            min_relevance_score, max_relevance_score, published_from, published_to,
        )
        is_postgres = self._database.bind is not None and self._database.bind.dialect.name == "postgresql"
        rank = cast(literal(None), Float).label("search_rank")

        if normalized_query and is_postgres:
            tsquery = func.plainto_tsquery("simple", func.unaccent(normalized_query))
            rank = func.ts_rank_cd(Content.search_vector, tsquery).cast(Float).label("search_rank")
            conditions.append(Content.search_vector.op("@@")(tsquery))
        elif normalized_query:
            # SQLite test compatibility only; production always executes PostgreSQL FTS above.
            term = f"%{normalized_query.lower()}%"
            searchable = func.lower(
                func.coalesce(Content.title, "") + " " + func.coalesce(Content.summary, "") + " " +
                func.coalesce(Content.category, "") + " " + cast(Content.topics, String) + " " + cast(Content.keywords, String)
            )
            conditions.append(searchable.like(term))
            rank = (
                case((func.lower(Content.title).like(term), 4.0), else_=0.0)
                + case((func.lower(Content.summary).like(term), 2.0), else_=0.0)
                + case((cast(Content.keywords, String).like(term), 3.0), else_=0.0)
                + case((cast(Content.topics, String).like(term), 3.0), else_=0.0)
                + case((func.lower(Content.category).like(term), 1.0), else_=0.0)
            ).cast(Float).label("search_rank")

        total = self._database.scalar(
            select(func.count()).select_from(select(Content.id).where(*conditions).subquery())
        ) or 0
        statement = select(Content, rank).where(*conditions)
        statement = self._order(statement, rank, normalized_query is not None, sort_by, sort_order)
        rows = self._database.execute(statement.offset((page - 1) * page_size).limit(page_size)).all()
        items = [self._to_item(content, search_rank) for content, search_rank in rows]
        return SearchResponse(
            query=normalized_query,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=ceil(total / page_size) if total else 0,
            items=items,
        )

    def _filters(
        self,
        source_id: UUID | None,
        category: str | None,
        topic: str | None,
        language: str | None,
        processing_status: str | None,
        min_relevance_score: int | None,
        max_relevance_score: int | None,
        published_from: datetime | None,
        published_to: datetime | None,
    ) -> list[object]:
        """Build only parameterized SQLAlchemy filter expressions."""

        conditions: list[object] = []
        is_postgres = self._database.bind is not None and self._database.bind.dialect.name == "postgresql"
        if source_id is not None:
            conditions.append(Content.source_id == source_id)
        if category is not None:
            conditions.append(Content.category == category)
        if topic is not None:
            if is_postgres:
                conditions.append(cast(Content.topics, JSONB).contains([topic]))
            else:
                conditions.append(cast(Content.topics, String).contains(f'"{topic}"'))
        if language is not None:
            conditions.append(Content.language == language)
        if processing_status is not None:
            conditions.append(Content.processing_status == processing_status)
        if min_relevance_score is not None:
            conditions.append(Content.relevance_score >= min_relevance_score)
        if max_relevance_score is not None:
            conditions.append(Content.relevance_score <= max_relevance_score)
        if published_from is not None:
            conditions.append(Content.published_at >= published_from)
        if published_to is not None:
            conditions.append(Content.published_at <= published_to)
        return conditions

    @staticmethod
    def _order(statement: object, rank: object, has_query: bool, sort_by: SearchSortBy | None, sort_order: SortOrder) -> object:
        """Apply the whitelisted deterministic ordering strategy."""

        descending = sort_order == SortOrder.DESC
        if sort_by is None:
            if has_query:
                return statement.order_by(rank.desc(), Content.relevance_score.desc().nullslast(), Content.published_at.desc().nullslast(), Content.created_at.desc())
            return statement.order_by(Content.created_at.desc())
        if sort_by == SearchSortBy.RANK:
            if not has_query:
                return statement.order_by(Content.created_at.desc())
            primary = rank.desc() if descending else rank.asc()
            return statement.order_by(primary, Content.relevance_score.desc().nullslast(), Content.published_at.desc().nullslast(), Content.created_at.desc())
        if sort_by == SearchSortBy.RELEVANCE:
            primary = Content.relevance_score.desc().nullslast() if descending else Content.relevance_score.asc().nullslast()
            return statement.order_by(primary, Content.created_at.desc())
        if sort_by == SearchSortBy.PUBLISHED_AT:
            primary = Content.published_at.desc().nullslast() if descending else Content.published_at.asc().nullslast()
            return statement.order_by(primary, Content.created_at.desc())
        primary = Content.created_at.desc() if descending else Content.created_at.asc()
        return statement.order_by(primary)

    @staticmethod
    def _to_item(content: Content, rank: float | None) -> SearchResultItem:
        """Map an ORM content record to a public search item without raw payload."""

        return SearchResultItem(
            id=content.id, source_id=content.source_id, title=content.title, url=content.url,
            summary=content.summary, author=content.author, published_at=content.published_at,
            language=content.language, category=content.category, topics=content.topics,
            keywords=content.keywords, relevance_score=content.relevance_score,
            processing_status=content.processing_status, created_at=content.created_at,
            search_rank=float(rank) if rank is not None else None,
        )

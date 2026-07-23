"""Read-only partial Content projection for the future reranking pipeline."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content


@dataclass(frozen=True, slots=True)
class RerankDocumentRecord:
    """Structured reranking text fields associated with one content identifier."""

    content_id: UUID
    title: str | None
    summary: str | None
    category: str | None
    topics: tuple[str, ...]
    keywords: tuple[str, ...]


class RerankDocumentRepository:
    """Hydrate ordered reranking fields in one query without applying eligibility."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def hydrate(self, content_ids: Sequence[UUID]) -> list[RerankDocumentRecord]:
        """Project existing content IDs once and restore the caller-provided ordering."""

        validated_ids = self._validate_content_ids(content_ids)
        if not validated_ids:
            return []
        rows = self._database.execute(
            select(
                Content.id,
                Content.title,
                Content.summary,
                Content.category,
                Content.topics,
                Content.keywords,
            ).where(Content.id.in_(validated_ids))
        )
        by_id = {
            content_id: RerankDocumentRecord(
                content_id=content_id,
                title=title,
                summary=summary,
                category=category,
                topics=self._coerce_terms(topics, "topics"),
                keywords=self._coerce_terms(keywords, "keywords"),
            )
            for content_id, title, summary, category, topics, keywords in rows
        }
        return [by_id[content_id] for content_id in validated_ids if content_id in by_id]

    @staticmethod
    def _validate_content_ids(content_ids: Sequence[UUID]) -> list[UUID]:
        if content_ids is None or isinstance(content_ids, (str, bytes)) or not isinstance(content_ids, Iterable):
            raise ValueError("content_ids must be an iterable of UUID values")
        values = list(content_ids)
        validated_ids: list[UUID] = []
        seen: set[UUID] = set()
        for content_id in values:
            if not isinstance(content_id, UUID):
                raise ValueError("content_ids must contain valid UUID values")
            if content_id in seen:
                raise ValueError("content_ids must not contain duplicates")
            seen.add(content_id)
            validated_ids.append(content_id)
        return validated_ids

    @staticmethod
    def _coerce_terms(value: object, name: str) -> tuple[str, ...]:
        """Convert observed JSON array values while rejecting incompatible persisted shapes."""

        if value is None:
            return ()
        if type(value) not in (list, tuple):
            raise ValueError(f"persisted {name} must be a list, tuple, or None")
        if any(type(item) is not str for item in value):
            raise ValueError(f"persisted {name} must contain only strings")
        return tuple(value)

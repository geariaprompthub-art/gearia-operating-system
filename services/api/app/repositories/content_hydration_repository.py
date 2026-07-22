"""Single-query content metadata hydration for ranked internal candidates."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content


@dataclass(frozen=True)
class HydratedContent:
    """Minimum safe content metadata used after retrieval ranking."""

    content_id: UUID
    title: str
    url: str
    summary: str | None


class ContentHydrationRepository:
    """Hydrate ranked IDs without relying on database IN-clause ordering."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def hydrate(self, content_ids: Sequence[UUID]) -> list[HydratedContent]:
        """Fetch all existing IDs once and preserve the caller-provided order."""

        validated_ids = self._validate_content_ids(content_ids)
        if not validated_ids:
            return []
        rows = self._database.execute(
            select(Content.id, Content.title, Content.url, Content.summary).where(Content.id.in_(validated_ids))
        )
        by_id = {
            content_id: HydratedContent(content_id=content_id, title=title, url=url, summary=summary)
            for content_id, title, url, summary in rows
        }
        return [by_id[content_id] for content_id in validated_ids if content_id in by_id]

    @staticmethod
    def _validate_content_ids(content_ids: Sequence[UUID]) -> list[UUID]:
        """Reject invalid or duplicate IDs before the repository issues any query."""

        if content_ids is None:
            raise ValueError("content_ids must not be None")
        validated_ids: list[UUID] = []
        seen: set[UUID] = set()
        for content_id in content_ids:
            if not isinstance(content_id, UUID):
                raise ValueError("content_ids must contain valid UUID values")
            if content_id in seen:
                raise ValueError("content_ids must not contain duplicates")
            seen.add(content_id)
            validated_ids.append(content_id)
        return validated_ids

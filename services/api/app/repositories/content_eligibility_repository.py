"""Read-only ordered filtering for the frozen Graph v1 content eligibility rule."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content import Content


class ContentEligibilityRepository:
    """Return existing processed content IDs without applying retrieval policy."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def filter_eligible(self, content_ids: Sequence[UUID]) -> list[UUID]:
        """Filter ordered IDs in one query while preserving their input order."""

        validated_ids = self._validate_content_ids(content_ids)
        if not validated_ids:
            return []
        eligible_ids = set(
            self._database.scalars(
                select(Content.id).where(
                    Content.id.in_(validated_ids),
                    Content.processing_status == "processed",
                )
            )
        )
        return [content_id for content_id in validated_ids if content_id in eligible_ids]

    @staticmethod
    def _validate_content_ids(content_ids: Sequence[UUID]) -> list[UUID]:
        """Reject malformed or duplicate IDs before issuing a database query."""

        if content_ids is None or isinstance(content_ids, (str, bytes)):
            raise ValueError("content_ids must be an iterable of UUID values")
        try:
            values = list(content_ids)
        except TypeError as error:
            raise ValueError("content_ids must be an iterable of UUID values") from error

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

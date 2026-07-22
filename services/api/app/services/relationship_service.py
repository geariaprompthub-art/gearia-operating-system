"""Deterministic-v1 relationship calculation and persistence service."""

from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
import logging
from math import ceil
import time
import unicodedata
from uuid import UUID

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.content import Content
from app.models.content_relationship import ContentRelationship
from app.schemas.relationship import (
    ContentRelationshipRead, RelatedContentItem, RelatedContentPage, RelationshipBatchRebuildResult,
    RelationshipBetweenResponse, RelationshipRebuildResult,
)
from app.schemas.search import SearchResultItem

logger = logging.getLogger(__name__)

DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION = "deterministic-v1"
MINIMUM_RELATIONSHIP_SCORE = 20.00
MAX_RELATIONSHIPS_PER_CONTENT = 50
MAX_CANDIDATES_PER_CONTENT = 500

TOPIC_WEIGHT = 35.00
KEYWORD_WEIGHT = 25.00
CATEGORY_WEIGHT = 15.00
TEXT_WEIGHT = 15.00
TEMPORAL_WEIGHT = 5.00
SOURCE_WEIGHT = 5.00


def normalize_token(value: object | None) -> str:
    """Return a stable accent-insensitive token without mutating persisted values."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(character for character in text if not unicodedata.combining(character))


def normalize_token_set(values: object | None) -> list[str]:
    """Normalize, discard blanks, deduplicate and sort arbitrary collection values."""

    if not isinstance(values, (list, tuple, set)):
        values = [] if values is None else [values]
    return sorted({token for value in values if (token := normalize_token(value))})


def jaccard_similarity(first: set[str], second: set[str]) -> float:
    """Calculate Jaccard similarity safely for two already normalized sets."""

    union = first | second
    return len(first & second) / len(union) if union else 0.0


class RelationshipService:
    """Build and query canonical content relationships without external services."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def rebuild_content(self, content_id: UUID, *, dry_run: bool = False) -> RelationshipRebuildResult:
        """Recalculate the bounded set of relationships incident to one processed content."""

        started = time.perf_counter()
        target = self._database.get(Content, content_id)
        if target is None:
            raise LookupError("Content not found")
        if target.processing_status != "processed":
            raise ValueError("Content must have processing_status=processed")

        candidates = self._candidates_for(target)
        qualified = [self._calculate(target, candidate) for candidate in candidates]
        qualified = [item for item in qualified if item["score"] >= MINIMUM_RELATIONSHIP_SCORE]
        qualified.sort(key=lambda item: (-item["score"], str(item["related"].id)))
        qualified = qualified[:MAX_RELATIONSHIPS_PER_CONTENT]
        desired = {self._canonical_pair(target.id, item["related"].id): item for item in qualified}
        existing = list(self._database.scalars(select(ContentRelationship).where(or_(ContentRelationship.content_id == target.id, ContentRelationship.related_content_id == target.id))))

        created = updated = unchanged = deleted = 0
        existing_by_pair = {(item.content_id, item.related_content_id): item for item in existing}
        for pair, calculated in desired.items():
            current = existing_by_pair.get(pair)
            if current is None:
                created += 1
                if not dry_run:
                    self._database.add(self._new_relationship(pair, calculated))
            elif self._relationship_changed(current, calculated):
                updated += 1
                if not dry_run:
                    self._apply_relationship(current, calculated)
            else:
                unchanged += 1

        stale = [item for pair, item in existing_by_pair.items() if pair not in desired]
        deleted = len(stale)
        if not dry_run:
            for item in stale:
                self._database.delete(item)
            self._database.commit()

        elapsed = int((time.perf_counter() - started) * 1000)
        result = RelationshipRebuildResult(
            content_id=content_id, candidates_evaluated=len(candidates), relationships_created=created,
            relationships_updated=updated, relationships_deleted=deleted,
            relationships_skipped=len(candidates) - len(qualified), relationships_unchanged=unchanged,
            duration_ms=elapsed, algorithm_version=DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION,
            dry_run=dry_run, errors=[],
        )
        logger.info("Relationship rebuild content_id=%s candidates=%s created=%s updated=%s deleted=%s dry_run=%s duration_ms=%s version=%s", content_id, len(candidates), created, updated, deleted, dry_run, elapsed, DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION)
        return result

    def rebuild_batch(self, **filters: object) -> RelationshipBatchRebuildResult:
        """Run a bounded deterministic rebuild over matching processed content records."""

        started = time.perf_counter()
        limit = int(filters.pop("limit", 100))
        dry_run = bool(filters.pop("dry_run", False))
        statement = select(Content).where(Content.processing_status == "processed")
        for field in ("source_id", "category", "processing_status"):
            if value := filters.get(field):
                statement = statement.where(getattr(Content, field) == value)
        if value := filters.get("published_after"):
            statement = statement.where(Content.published_at >= value)
        if value := filters.get("published_before"):
            statement = statement.where(Content.published_at <= value)
        contents = list(self._database.scalars(statement.order_by(Content.created_at.asc(), Content.id.asc()).limit(limit)))
        totals = {key: 0 for key in ("candidates_evaluated", "relationships_created", "relationships_updated", "relationships_deleted", "relationships_skipped", "relationships_unchanged")}
        errors: list[dict[str, str]] = []
        for content in contents:
            try:
                result = self.rebuild_content(content.id, dry_run=dry_run)
                for key in totals:
                    totals[key] += getattr(result, key)
            except (ValueError, LookupError) as error:
                errors.append({"content_id": str(content.id), "error": str(error)})
                self._database.rollback()
        return RelationshipBatchRebuildResult(
            contents_processed=len(contents), contents_succeeded=len(contents) - len(errors), contents_failed=len(errors),
            duration_ms=int((time.perf_counter() - started) * 1000),
            algorithm_version=DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION, dry_run=dry_run, errors=errors, **totals,
        )

    def related(self, content_id: UUID, *, page: int, page_size: int, min_score: float | None, category: str | None, source_id: UUID | None, exclude_same_source: bool, algorithm_version: str | None, descending: bool) -> RelatedContentPage:
        """Return a paginated view of the other side of each incident relationship."""

        target = self._database.get(Content, content_id)
        if target is None:
            raise LookupError("Content not found")
        relationships = list(self._database.scalars(select(ContentRelationship).where(or_(ContentRelationship.content_id == content_id, ContentRelationship.related_content_id == content_id))))
        items: list[RelatedContentItem] = []
        for relationship in relationships:
            other_id = relationship.related_content_id if relationship.content_id == content_id else relationship.content_id
            other = self._database.get(Content, other_id)
            if other is None or (min_score is not None and float(relationship.score) < min_score):
                continue
            if category is not None and other.category != category:
                continue
            if source_id is not None and other.source_id != source_id:
                continue
            if exclude_same_source and other.source_id == target.source_id:
                continue
            if algorithm_version is not None and relationship.algorithm_version != algorithm_version:
                continue
            items.append(RelatedContentItem(content=self._content_item(other), relationship=ContentRelationshipRead.model_validate(relationship)))
        # Apply tie-breakers independently so the content UUID stays ASC in both directions.
        items.sort(key=lambda item: str(item.content.id))
        items.sort(key=lambda item: item.content.published_at or datetime.min.replace(tzinfo=UTC), reverse=descending)
        items.sort(key=lambda item: float(item.relationship.score), reverse=descending)
        total = len(items)
        start = (page - 1) * page_size
        return RelatedContentPage(content_id=content_id, page=page, page_size=page_size, total=total, total_pages=ceil(total / page_size) if total else 0, items=items[start:start + page_size])

    def between(self, first_id: UUID, second_id: UUID) -> RelationshipBetweenResponse:
        """Look up one persisted pair in either URL order."""

        first = self._database.get(Content, first_id)
        second = self._database.get(Content, second_id)
        if first is None or second is None:
            raise LookupError("Content not found")
        pair = self._canonical_pair(first_id, second_id)
        relationship = self._database.scalar(select(ContentRelationship).where(ContentRelationship.content_id == pair[0], ContentRelationship.related_content_id == pair[1]))
        if relationship is None:
            raise LookupError("Content relationship not found")
        return RelationshipBetweenResponse(relationship=ContentRelationshipRead.model_validate(relationship), content=self._content_item(first), related_content=self._content_item(second))

    def _candidates_for(self, target: Content) -> list[Content]:
        """Retrieve only preliminarily similar processed candidates, bounded at the database."""

        signals: list[object] = []
        is_postgres = self._database.bind is not None and self._database.bind.dialect.name == "postgresql"
        category = normalize_token(target.category)
        if category:
            signals.append(Content.category == target.category)
        for token in normalize_token_set(target.topics):
            signals.append(cast(Content.topics, JSONB).contains([token]) if is_postgres else cast(Content.topics, String).contains(f'"{token}"'))
        for token in normalize_token_set(target.keywords):
            signals.append(cast(Content.keywords, JSONB).contains([token]) if is_postgres else cast(Content.keywords, String).contains(f'"{token}"'))
        if target.source_id is not None:
            signals.append(Content.source_id == target.source_id)
        if not signals:
            return []
        return list(self._database.scalars(select(Content).where(Content.processing_status == "processed", Content.id != target.id, or_(*signals)).order_by(Content.created_at.asc(), Content.id.asc()).limit(MAX_CANDIDATES_PER_CONTENT)))

    def _calculate(self, target: Content, related: Content) -> dict[str, object]:
        """Produce the full explainable score for a target/candidate pair."""

        topics_a, topics_b = set(normalize_token_set(target.topics)), set(normalize_token_set(related.topics))
        keywords_a, keywords_b = set(normalize_token_set(target.keywords)), set(normalize_token_set(related.keywords))
        shared_topics, shared_keywords = sorted(topics_a & topics_b), sorted(keywords_a & keywords_b)
        same_category = bool(normalize_token(target.category) and normalize_token(target.category) == normalize_token(related.category))
        same_source = target.source_id == related.source_id
        text_similarity = self._text_similarity(target, related)
        distance = abs((target.published_at - related.published_at).days) if target.published_at and related.published_at else None
        temporal = TEMPORAL_WEIGHT if distance is not None and distance <= 7 else 3.0 if distance is not None and distance <= 30 else 1.0 if distance is not None and distance <= 90 else 0.0
        breakdown = {"topics": round(jaccard_similarity(topics_a, topics_b) * TOPIC_WEIGHT, 2), "keywords": round(jaccard_similarity(keywords_a, keywords_b) * KEYWORD_WEIGHT, 2), "category": CATEGORY_WEIGHT if same_category else 0.0, "text": round(text_similarity * TEXT_WEIGHT, 2), "temporal": temporal, "source": SOURCE_WEIGHT if same_source else 0.0}
        reasons = []
        if shared_topics: reasons.append("shared_topics")
        if shared_keywords: reasons.append("shared_keywords")
        if same_category: reasons.append("same_category")
        if text_similarity > 0: reasons.append("text_similarity")
        if temporal > 0: reasons.append("temporal_proximity")
        if same_source: reasons.append("same_source")
        return {"related": related, "score": round(sum(breakdown.values()), 2), "score_breakdown": breakdown, "shared_topics": shared_topics, "shared_keywords": shared_keywords, "same_category": same_category, "same_source": same_source, "text_similarity": round(text_similarity, 5), "published_distance_days": distance, "reasons": reasons}

    def _text_similarity(self, first: Content, second: Content) -> float:
        """Use pg_trgm in PostgreSQL and a stdlib fallback exclusively for SQLite tests."""

        first_text, second_text = normalize_token(f"{first.title} {first.summary or ''}"), normalize_token(f"{second.title} {second.summary or ''}")
        if self._database.bind is not None and self._database.bind.dialect.name == "postgresql":
            value = self._database.scalar(select(func.similarity(func.unaccent(first_text), func.unaccent(second_text))))
            return max(0.0, min(1.0, float(value or 0.0)))
        return SequenceMatcher(None, first_text, second_text).ratio()

    @staticmethod
    def _canonical_pair(first: UUID, second: UUID) -> tuple[UUID, UUID]:
        """Canonicalize unordered UUID pairs before every read or write."""

        if first == second:
            raise ValueError("A content relationship cannot reference the same content")
        return (first, second) if str(first) < str(second) else (second, first)

    def _new_relationship(self, pair: tuple[UUID, UUID], values: dict[str, object]) -> ContentRelationship:
        """Create an ORM relationship from a calculated score record."""

        return ContentRelationship(content_id=pair[0], related_content_id=pair[1], algorithm_version=DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION, calculated_at=datetime.now(UTC), **{key: value for key, value in values.items() if key != "related"})

    def _apply_relationship(self, relationship: ContentRelationship, values: dict[str, object]) -> None:
        """Update recalculated fields while retaining the original created_at timestamp."""

        for key, value in values.items():
            if key != "related":
                setattr(relationship, key, value)
        relationship.algorithm_version = DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION
        relationship.calculated_at = datetime.now(UTC)

    @staticmethod
    def _relationship_changed(relationship: ContentRelationship, values: dict[str, object]) -> bool:
        """Avoid writes and timestamp changes when a deterministic rebuild is identical."""

        for key, value in values.items():
            if key == "related":
                continue
            current = getattr(relationship, key)
            # Numeric columns round-trip as Decimal, while calculation uses stable floats.
            if key in {"score", "text_similarity"}:
                if round(float(current), 5) != round(float(value), 5):
                    return True
            elif current != value:
                return True
        return relationship.algorithm_version != DETERMINISTIC_RELATIONSHIP_ALGORITHM_VERSION

    @staticmethod
    def _content_item(content: Content) -> SearchResultItem:
        """Map a content record into the existing lightweight public content contract."""

        return SearchResultItem(id=content.id, source_id=content.source_id, title=content.title, url=content.url, summary=content.summary, author=content.author, published_at=content.published_at, language=content.language, category=content.category, topics=content.topics, keywords=content.keywords, relevance_score=content.relevance_score, processing_status=content.processing_status, created_at=content.created_at, search_rank=None)

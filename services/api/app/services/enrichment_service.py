"""Deterministic content enrichment and classification service."""

import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enrichment_rules import (
    AI_TERMS,
    AUTOMATION_TERMS,
    CATEGORY_PRIORITY,
    CATEGORY_RULES,
    MARKETING_TERMS,
    PROMPT_TERMS,
    STOPWORDS,
    TOPIC_RULES,
)
from app.models.content import Content

logger = logging.getLogger(__name__)


def normalize_text(*values: str | None) -> str:
    """Combine text values into an accent-insensitive normalized string."""

    combined = " ".join(value or "" for value in values)
    without_accents = "".join(
        character
        for character in unicodedata.normalize("NFD", combined)
        if unicodedata.category(character) != "Mn"
    )
    return " ".join(without_accents.lower().split())


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    """Return whether a normalized text contains at least one normalized term."""

    return any(normalize_text(term) in text for term in terms)


class EnrichmentService:
    """Enrich Content records using centralized deterministic rules."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def enrich_content(self, content: Content) -> bool:
        """Process one content item and isolate failures in its persisted state."""

        try:
            content.processing_status = "processing"
            text = normalize_text(content.title, content.summary)
            title = normalize_text(content.title)
            summary = normalize_text(content.summary)
            topics = self.identify_topics(text)
            content.category = self.identify_category(text)
            content.topics = topics
            content.keywords = self.extract_keywords(text)
            content.relevance_score = self.calculate_relevance(title, summary, topics, content.published_at)
            content.processing_status = "processed"
            content.processing_error = None
            content.processed_at = datetime.now(UTC)
            self._database.commit()
            self._database.refresh(content)
            return True
        except Exception as error:  # noqa: BLE001 - failures must be isolated per record
            logger.exception("Failed to enrich content %s", content.id)
            self._database.rollback()
            content.processing_status = "failed"
            content.processing_error = str(error)[:500] or "Content enrichment failed"
            content.processed_at = datetime.now(UTC)
            self._database.commit()
            return False

    def run_pending(self, limit: int) -> tuple[int, int, int]:
        """Enrich pending content in stable oldest-first order."""

        contents = list(
            self._database.scalars(
                select(Content)
                .where(Content.processing_status == "pending")
                .order_by(Content.created_at.asc())
                .limit(limit)
            )
        )
        processed = 0
        failed = 0
        for content in contents:
            if self.enrich_content(content):
                processed += 1
            else:
                failed += 1
        return len(contents), processed, failed

    @staticmethod
    def identify_category(text: str) -> str:
        """Return the first matching category according to configured priority."""

        for category in CATEGORY_PRIORITY:
            if _contains_any(text, CATEGORY_RULES[category]):
                return category
        return "outros"

    @staticmethod
    def identify_topics(text: str) -> list[str]:
        """Return stable, unique topics whose configured rules match."""

        return [topic for topic, terms in TOPIC_RULES.items() if _contains_any(text, terms)]

    @staticmethod
    def extract_keywords(text: str) -> list[str]:
        """Extract up to ten stable keywords, prioritizing configured matching terms."""

        keywords: list[str] = []
        known_terms = tuple(term for terms in CATEGORY_RULES.values() for term in terms) + tuple(
            term for terms in TOPIC_RULES.values() for term in terms
        )
        for term in known_terms:
            normalized_term = normalize_text(term)
            if normalized_term in text and normalized_term not in keywords:
                keywords.append(normalized_term)
        for token in re.findall(r"[a-z0-9][a-z0-9.-]*", text):
            if len(token) >= 3 and token not in STOPWORDS and token not in keywords:
                keywords.append(token)
        return keywords[:10]

    @staticmethod
    def calculate_relevance(
        title: str, summary: str, topics: list[str], published_at: datetime | None
    ) -> int:
        """Calculate a deterministic relevance score constrained to 0 through 100."""

        score = 0
        if _contains_any(title, AI_TERMS):
            score += 20
        if _contains_any(summary, AI_TERMS):
            score += 10
        if _contains_any(title, PROMPT_TERMS):
            score += 20
        if _contains_any(title, AUTOMATION_TERMS):
            score += 15
        if _contains_any(title, MARKETING_TERMS):
            score += 10
        score += len(topics) * 5
        if summary:
            score += 5
        if published_at is not None:
            reference = datetime.now(UTC)
            published = published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC)
            if reference - published <= timedelta(days=30):
                score += 10
        return max(0, min(100, score))

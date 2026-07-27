"""RSS ingestion service for Scout Engine v1."""

import hashlib
import json
import logging
from calendar import timegm
from datetime import UTC, datetime
from typing import Any

import feedparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.content import Content
from app.models.source import Source
from app.services.safe_rss_fetcher import SafeRSSFetchError, SafeRSSFetcher

logger = logging.getLogger(__name__)


class ScoutService:
    """Fetch enabled RSS sources and persist deduplicated content."""

    def __init__(self, database: Session) -> None:
        self._database = database
        self._fetcher = SafeRSSFetcher(get_settings())

    def run(self) -> tuple[int, int]:
        """Ingest all enabled RSS sources with a configured URL."""

        statement = select(Source).where(
            Source.enabled.is_(True), Source.type == "rss", Source.url.is_not(None)
        )
        sources = list(self._database.scalars(statement))
        self._database.commit()
        created_count = 0
        for source in sources:
            try:
                created_count += self._ingest_source(source)
                self._database.commit()
            except SafeRSSFetchError:
                self._database.rollback()
                logger.warning("RSS source fetch was rejected or unavailable", extra={"source_id": str(source.id)})
            except Exception:
                self._database.rollback()
                logger.exception("RSS source ingestion failed", extra={"source_id": str(source.id)})
        return len(sources), created_count

    def _ingest_source(self, source: Source) -> int:
        """Fetch and persist new entries for a single RSS source."""

        if source.url is None:
            return 0

        logger.info("Fetching RSS source %s", source.id)
        parsed_feed = feedparser.parse(self._fetcher.fetch(source.url))
        feed_language = parsed_feed.get("feed", {}).get("language")
        created_count = 0

        for entry in parsed_feed.get("entries", [])[: get_settings().scout_max_entries_per_feed]:
            content = self._normalize_entry(source, entry, feed_language)
            exists = self._database.scalar(
                select(Content.id).where(Content.fingerprint == content.fingerprint)
            )
            if exists is not None:
                continue
            self._database.add(content)
            self._database.flush()
            created_count += 1

        return created_count

    def _normalize_entry(
        self, source: Source, entry: dict[str, Any], feed_language: str | None
    ) -> Content:
        """Map a feed entry to the normalized Content model."""

        published_at = self._parse_published_at(entry)
        title = str(entry.get("title", "")).strip()
        url = str(entry.get("link", "")).strip()
        fingerprint = self._make_fingerprint(source.id, title, url, published_at)
        raw_payload = json.loads(json.dumps(entry, default=str))

        return Content(
            source_id=source.id,
            title=title,
            url=url,
            summary=entry.get("summary") or entry.get("description"),
            author=entry.get("author"),
            published_at=published_at,
            language=entry.get("language") or feed_language,
            fingerprint=fingerprint,
            raw_payload=raw_payload,
        )

    @staticmethod
    def _parse_published_at(entry: dict[str, Any]) -> datetime | None:
        """Convert feedparser's publication tuple to an aware datetime."""

        value = entry.get("published_parsed") or entry.get("updated_parsed")
        if value is None:
            return None
        return datetime.fromtimestamp(timegm(value), UTC)

    @staticmethod
    def _make_fingerprint(
        source_id: object, title: str, url: str, published_at: datetime | None
    ) -> str:
        """Return a stable SHA-256 fingerprint for one normalized entry."""

        value = "|".join((str(source_id), title, url, published_at.isoformat() if published_at else ""))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

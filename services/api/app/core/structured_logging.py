"""Fail-open JSON logging primitives for new observability events."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.core.correlation import correlation_context

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
MAX_DEPTH = "[MAX_DEPTH]"
CYCLE = "[CYCLE]"


class KeySanitizer:
    """Recognize sensitive field names without inspecting their values."""

    sensitive_keys = frozenset({"authorization", "api_key", "apikey", "token", "access_token", "refresh_token", "password", "secret", "cookie", "set-cookie", "connection_string", "database_url"})

    def is_sensitive(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self.sensitive_keys


class ValueLimiter:
    """Bound untrusted values without invoking arbitrary representations."""

    def __init__(self, max_depth: int = 5, max_string: int = 512, max_items: int = 50) -> None:
        self.max_depth, self.max_string, self.max_items = max_depth, max_string, max_items

    def limit(self, value: Any, depth: int = 0, seen: set[int] | None = None) -> Any:
        if depth > self.max_depth:
            return MAX_DEPTH
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if value in {REDACTED, TRUNCATED, MAX_DEPTH, CYCLE} or value.startswith("[UNSERIALIZABLE:"):
                return value
            return value if len(value) <= self.max_string else value[: self.max_string] + TRUNCATED
        seen = seen if seen is not None else set()
        if isinstance(value, (dict, list, tuple)):
            marker = id(value)
            if marker in seen:
                return CYCLE
            seen.add(marker)
            try:
                if isinstance(value, dict):
                    return {str(key): self.limit(item, depth + 1, seen) for key, item in list(value.items())[: self.max_items]}
                return [self.limit(item, depth + 1, seen) for item in list(value)[: self.max_items]]
            finally:
                seen.remove(marker)
        return f"[UNSERIALIZABLE:{type(value).__name__}]"


class StructuredSanitizer:
    """Combine redaction and limits while preserving source objects."""

    def __init__(self, key_sanitizer: KeySanitizer | None = None, limiter: ValueLimiter | None = None) -> None:
        self._keys = key_sanitizer or KeySanitizer()
        self._limiter = limiter or ValueLimiter()

    def sanitize(self, value: Any) -> Any:
        return self._sanitize(value, set())

    def _sanitize(self, value: Any, seen: set[int]) -> Any:
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return CYCLE
            seen.add(marker)
            try:
                safe = {str(key): REDACTED if self._keys.is_sensitive(key) else self._sanitize(item, seen) for key, item in value.items()}
            finally:
                seen.remove(marker)
            return self._limiter.limit(safe)
        if isinstance(value, (list, tuple)):
            marker = id(value)
            if marker in seen:
                return CYCLE
            seen.add(marker)
            try:
                safe = [self._sanitize(item, seen) for item in value]
            finally:
                seen.remove(marker)
            return self._limiter.limit(safe)
        return self._limiter.limit(value)


class StructuredLogFormatter(logging.Formatter):
    """Render a single JSON line without raw exception payloads."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(UTC).isoformat(), "level": record.levelname, "logger": record.name, "event": getattr(record, "event", "log"), "message": record.getMessage().replace("\n", " ").replace("\r", " "), "request_id": getattr(record, "request_id", None)}
        reserved = set(payload)
        payload.update({key: value for key, value in getattr(record, "structured_fields", {}).items() if key not in reserved and value is not None})
        return json.dumps({key: value for key, value in payload.items() if value is not None}, ensure_ascii=False, default=lambda value: f"[UNSERIALIZABLE:{type(value).__name__}]")


class SafeStructuredLogger:
    """Central fail-open boundary for structured events."""

    def __init__(self, logger: logging.Logger, sanitizer: StructuredSanitizer | None = None) -> None:
        self._logger, self._sanitizer = logger, sanitizer or StructuredSanitizer()

    def debug(self, event: str, message: str, **fields: Any) -> None: self._emit(logging.DEBUG, event, message, fields)
    def info(self, event: str, message: str, **fields: Any) -> None: self._emit(logging.INFO, event, message, fields)
    def warning(self, event: str, message: str, **fields: Any) -> None: self._emit(logging.WARNING, event, message, fields)
    def error(self, event: str, message: str, **fields: Any) -> None: self._emit(logging.ERROR, event, message, fields)

    def _emit(self, level: int, event: str, message: str, fields: dict[str, Any]) -> None:
        try:
            extra = {"event": event, "request_id": correlation_context.get(), "structured_fields": self._sanitizer.sanitize(fields)}
            self._logger.log(level, message, extra=extra)
        except Exception:
            return None

"""Foundation coverage for correlation and safe structured logging."""

import json
import logging
import asyncio
from io import StringIO

from app.core.correlation import correlation_context, resolve_request_id
from app.core.structured_logging import REDACTED, SafeStructuredLogger, StructuredLogFormatter, StructuredSanitizer, ValueLimiter
from test_main import client


def test_correlation_context_restores_previous_value() -> None:
    outer = correlation_context.set("outer")
    inner = correlation_context.set("inner")
    correlation_context.reset(inner)
    assert correlation_context.get() == "outer"
    correlation_context.reset(outer)
    assert correlation_context.get() is None


def test_request_id_policy_preserves_only_safe_values() -> None:
    assert resolve_request_id("request-123_A.b") == "request-123_A.b"
    generated = resolve_request_id("bad value\nsecret")
    assert generated != "bad value\nsecret" and len(generated) == 36
    assert resolve_request_id("x" * 129) != "x" * 129


def test_sanitizer_redacts_recursively_limits_values_and_preserves_input() -> None:
    source = {"Authorization": "SUPER_SECRET_API_KEY_123", "nested": [{"token": "AUTH_TOKEN_789"}], "long": "x" * 20}
    safe = StructuredSanitizer(limiter=ValueLimiter(max_string=5)).sanitize(source)
    assert safe["Authorization"] == REDACTED
    assert safe["nested"][0]["token"] == REDACTED
    assert safe["long"].endswith("[TRUNCATED]")
    assert source["Authorization"] == "SUPER_SECRET_API_KEY_123"


def test_structured_formatter_and_safe_logger_emit_one_sanitized_json_line() -> None:
    stream = StringIO()
    logger = logging.getLogger("structured-test")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    token = correlation_context.set("request-123")
    try:
        SafeStructuredLogger(logger).info("http_request_started", "hello\nworld", api_key="SUPER_SECRET_API_KEY_123")
    finally:
        correlation_context.reset(token)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["request_id"] == "request-123"
    assert payload["api_key"] == REDACTED
    assert "SUPER_SECRET_API_KEY_123" not in lines[0]


def test_safe_logger_absorbs_handler_failures() -> None:
    class BrokenLogger:
        def log(self, *args: object, **kwargs: object) -> None: raise RuntimeError("broken")
    assert SafeStructuredLogger(BrokenLogger()).info("event", "message", secret="x") is None  # type: ignore[arg-type]


def test_http_middleware_preserves_or_generates_safe_request_id() -> None:
    preserved = client.get("/health", headers={"X-Request-ID": "request-123"})
    generated = client.get("/health", headers={"X-Request-ID": "bad value"})
    assert preserved.headers["X-Request-ID"] == "request-123"
    assert generated.headers["X-Request-ID"] != "bad value"
    assert len(generated.headers["X-Request-ID"]) == 36


def test_contextvar_isolated_across_concurrent_tasks() -> None:
    async def read(request_id: str) -> str | None:
        token = correlation_context.set(request_id)
        try:
            await asyncio.sleep(0)
            return correlation_context.get()
        finally:
            correlation_context.reset(token)
    async def run_all() -> list[str | None]:
        return await asyncio.gather(*[read(f"concurrent-{index}") for index in range(20)])
    assert asyncio.run(run_all()) == [f"concurrent-{index}" for index in range(20)]
    assert correlation_context.get() is None


def test_formatter_protects_base_fields_and_handles_cycles_without_repr() -> None:
    stream = StringIO()
    logger = logging.getLogger("structured-base-fields")
    logger.handlers.clear(); logger.setLevel(logging.INFO); logger.propagate = False
    handler = logging.StreamHandler(stream); handler.setFormatter(StructuredLogFormatter()); logger.addHandler(handler)
    cycle: dict[str, object] = {}; cycle["self"] = cycle
    fields = StructuredSanitizer().sanitize({"event": "forged", "level": "CRITICAL", "logger": "attacker", "request_id": "forged", "payload": cycle})
    logger.info("approved message", extra={"event": "approved_event", "request_id": "approved-request", "structured_fields": fields})
    payload = json.loads(stream.getvalue())
    assert payload["event"] == "approved_event" and payload["level"] == "INFO" and payload["logger"] == "structured-base-fields" and payload["request_id"] == "approved-request"
    assert payload["payload"]["self"] == "[CYCLE]"


def test_final_json_redacts_sensitive_markers_and_neutralizes_injection() -> None:
    stream = StringIO(); logger = logging.getLogger("structured-security")
    logger.handlers.clear(); logger.setLevel(logging.INFO); logger.propagate = False
    handler = logging.StreamHandler(stream); handler.setFormatter(StructuredLogFormatter()); logger.addHandler(handler)
    SafeStructuredLogger(logger).info("approved", "legitimate\nforged second line", api_key="SUPER_SECRET_API_KEY_123", nested={"Authorization": "AUTH_TOKEN_789", "password": "PASSWORD_VALUE_XYZ", "database_url": "DATABASE_URL_SECRET_999", "cookie": "COOKIE_SECRET_777"})
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["api_key"] == REDACTED and payload["nested"]["Authorization"] == REDACTED
    assert all(marker not in lines[0] for marker in ("SUPER_SECRET_API_KEY_123", "AUTH_TOKEN_789", "PASSWORD_VALUE_XYZ", "DATABASE_URL_SECRET_999", "COOKIE_SECRET_777"))


def test_limiter_handles_cyclic_list_and_unknown_object_without_repr() -> None:
    class Dangerous:
        def __repr__(self) -> str: raise RuntimeError("must not be called")
    cycle: list[object] = []; cycle.append(cycle)
    result = StructuredSanitizer().sanitize({"cycle": cycle, "object": Dangerous(), "many": list(range(100))})
    assert result["cycle"][0] == "[CYCLE]"
    assert result["object"] == "[UNSERIALIZABLE:Dangerous]"
    assert len(result["many"]) == 50

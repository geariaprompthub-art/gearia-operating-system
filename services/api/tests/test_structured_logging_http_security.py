"""HTTP-level guarantees that request logging never includes request contents."""

import json
import logging
from io import StringIO

from fastapi.testclient import TestClient

from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.main import create_app


def test_http_logging_emits_only_safe_request_metadata_and_one_json_line_per_event() -> None:
    """The middleware logs method, route and status instead of headers, body or query data."""

    markers = (
        "PRIVATE_QUERY_CONTENT_456",
        "SUPER_SECRET_API_KEY_123",
        "AUTH_TOKEN_789",
        "COOKIE_SECRET_777",
        "DOCUMENT_CONTENT_ABC",
        "RAW_PROVIDER_RESPONSE_555",
        "PROMPT_SECRET_888",
        "VECTOR_SECRET_444",
        "BODY_SECRET_333",
        "HEADER_SECRET_222",
    )
    stream = StringIO()
    raw_logger = logging.getLogger("http-security-capture")
    raw_logger.handlers.clear()
    raw_logger.setLevel(logging.INFO)
    raw_logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    raw_logger.addHandler(handler)
    app = create_app(structured_logger=SafeStructuredLogger(raw_logger))
    try:
        response = TestClient(app).request(
            "POST",
            "/health",
            params={"query": markers[0]},
            content=markers[8],
            headers={
                "Authorization": f"Bearer {markers[1]}",
                "Cookie": f"session={markers[3]}",
                "X-Custom-Secret": markers[9],
                "X-Request-ID": 'bad"}\\n{"event":"forged_event"}',
            },
        )
        lines = [line for line in stream.getvalue().splitlines() if line]
    finally:
        raw_logger.handlers.clear()

    assert response.status_code == 405
    assert response.headers["X-Request-ID"] != 'bad"}\\n{"event":"forged_event"}'
    assert len(lines) == 2
    payloads = [json.loads(line) for line in lines]
    assert [payload["event"] for payload in payloads] == ["http_request_started", "http_request_completed"]
    assert all(marker not in "\n".join(lines) for marker in markers)
    assert all(payload["route"] == "/health" for payload in payloads)

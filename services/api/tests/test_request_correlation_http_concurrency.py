"""Integrated ASGI coverage for request-correlation isolation."""

import asyncio
import json
import logging
from io import StringIO
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.core.correlation import REQUEST_ID_HEADER, correlation_context
from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.main import create_app


def _app_with_barrier(expected: int) -> tuple[object, StringIO, logging.Logger]:
    """Create an isolated app whose test route proves concurrent overlap."""

    stream = StringIO()
    raw_logger = logging.getLogger(f"correlation-concurrency-{id(stream)}")
    raw_logger.handlers.clear()
    raw_logger.setLevel(logging.INFO)
    raw_logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    raw_logger.addHandler(handler)
    app = create_app(structured_logger=SafeStructuredLogger(raw_logger))
    lock = asyncio.Lock()
    arrived = 0
    release = asyncio.Event()

    @app.get("/_test/correlation")
    async def correlation_probe(_: Request) -> dict[str, str | None]:
        nonlocal arrived
        async with lock:
            arrived += 1
            if arrived == expected:
                release.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        return {"request_id": correlation_context.get()}

    return app, stream, raw_logger


async def _run_requests(headers: list[dict[str, str]]) -> tuple[list[httpx.Response], list[dict[str, object]]]:
    app, stream, raw_logger = _app_with_barrier(len(headers))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*[client.get("/_test/correlation", headers=header) for header in headers])
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        return responses, events
    finally:
        raw_logger.handlers.clear()


def _events_by_request(events: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in events:
        grouped.setdefault(str(event["request_id"]), []).append(event)
    return grouped


def test_real_http_concurrency_preserves_valid_request_ids_and_restores_context() -> None:
    """Twenty overlapping ASGI requests retain their own ContextVar values."""

    identifiers = [f"http-valid-{index:03d}" for index in range(20)]
    responses, events = asyncio.run(_run_requests([{REQUEST_ID_HEADER: value} for value in identifiers]))
    grouped = _events_by_request(events)

    assert [response.status_code for response in responses] == [200] * 20
    assert {response.json()["request_id"] for response in responses} == set(identifiers)
    assert {response.headers[REQUEST_ID_HEADER] for response in responses} == set(identifiers)
    assert set(grouped) == set(identifiers)
    assert all([event["event"] for event in grouped[value]] == ["http_request_started", "http_request_completed"] for value in identifiers)
    assert correlation_context.get() is None


def test_real_http_concurrency_generates_unique_ids_for_missing_or_invalid_headers() -> None:
    """Generated IDs stay isolated and invalid header values never reach events."""

    invalid = [" ", "bad\nvalue", "bad\rvalue", "bad\tvalue", "x" * 129, "bad/value"]
    headers = [{} for _ in range(14)] + [{REQUEST_ID_HEADER: value} for value in invalid]
    responses, events = asyncio.run(_run_requests(headers))
    identifiers = [response.headers[REQUEST_ID_HEADER] for response in responses]

    assert len(identifiers) == len(set(identifiers)) == 20
    assert all(str(UUID(value)) == value for value in identifiers)
    assert {response.json()["request_id"] for response in responses} == set(identifiers)
    emitted = "\n".join(json.dumps(event) for event in events)
    assert all(value not in emitted for value in invalid if value.strip())
    assert correlation_context.get() is None


def test_real_http_concurrency_mixes_preserved_and_generated_ids_without_leakage() -> None:
    """Mixed accepted, absent and rejected inputs share one real middleware barrier."""

    valid = [f"mixed-valid-{index:03d}" for index in range(8)]
    headers = (
        [{REQUEST_ID_HEADER: value} for value in valid]
        + [{} for _ in range(6)]
        + [{REQUEST_ID_HEADER: value} for value in ("bad/value", "x" * 129, "bad value", "bad\tvalue", "bad\nvalue", "bad\rvalue")]
    )
    responses, events = asyncio.run(_run_requests(headers))
    identifiers = [response.headers[REQUEST_ID_HEADER] for response in responses]
    grouped = _events_by_request(events)

    assert set(valid).issubset(identifiers)
    assert len(identifiers) == len(set(identifiers)) == 20
    assert set(grouped) == set(identifiers)
    assert all(len(grouped[value]) == 2 for value in identifiers)
    assert correlation_context.get() is None


@pytest.mark.parametrize("status_code", [200, 500, 502, 503])
def test_http_context_is_restored_after_success_and_error_contracts(status_code: int) -> None:
    """Token reset restores the caller context after every response path."""

    app, _, raw_logger = _app_with_barrier(1)
    observed: list[str | None] = []

    @app.get(f"/_test/status/{status_code}")
    async def status_probe() -> dict[str, str]:
        observed.append(correlation_context.get())
        if status_code != 200:
            raise HTTPException(status_code=status_code, detail="controlled")
        return {"status": "ok"}

    outer = correlation_context.set("outer-context")
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            f"/_test/status/{status_code}",
            headers={REQUEST_ID_HEADER: f"status-{status_code}"},
        )
        assert response.status_code == status_code
        assert observed == [f"status-{status_code}"]
        assert correlation_context.get() == "outer-context"
    finally:
        correlation_context.reset(outer)
        raw_logger.handlers.clear()

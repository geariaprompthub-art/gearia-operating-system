"""Public P2B registration contract with no real email provider."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter
from app.services.auth_dependencies import get_registration_application_service
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.rate_limiter import RateLimitDecision, RateLimitPolicy
from app.services.registration_application_service import (
    RegistrationApplicationService,
)
from app.services.registration_service import RegistrationError, RegistrationResult


class ControlledRateLimiter:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[RateLimitPolicy, str]] = []

    def consume(self, policy: RateLimitPolicy, identifier: str) -> RateLimitDecision:
        self.calls.append((policy, identifier))
        return RateLimitDecision(self.allowed, 1 if self.allowed else 0, 100, 42 if not self.allowed else 0)


@dataclass
class ControlledRegistrationService:
    outcome: str = "created"
    calls: list[tuple[str, str]] | None = None
    events: list[str] | None = None

    def __post_init__(self) -> None:
        self.calls = [] if self.calls is None else self.calls
        self.events = [] if self.events is None else self.events

    def register(self, email: str, password: str) -> RegistrationResult:
        self.calls.append((email, password))
        if self.outcome == "hidden":
            raise RegistrationError("registration unavailable")
        if self.outcome == "failure":
            raise RuntimeError("controlled registration failure")
        self.events.append("registration_committed")
        return RegistrationResult(
            user_id=uuid4(),
            workspace_id=uuid4(),
            raw_verification_token="test-only-raw-verification-token",
            verification_expires_at=datetime.now(UTC) + timedelta(hours=24),
            registration_state=self.outcome,
        )


@pytest.fixture(autouse=True)
def clean_register_overrides() -> None:
    original = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


def _service(
    registration: ControlledRegistrationService | None = None,
    limiter: ControlledRateLimiter | None = None,
    adapter: FakeEmailDeliveryAdapter | None = None,
    structured_logger: SafeStructuredLogger | None = None,
) -> tuple[RegistrationApplicationService, ControlledRegistrationService, ControlledRateLimiter, FakeEmailDeliveryAdapter]:
    controlled_registration = registration or ControlledRegistrationService()
    controlled_limiter = limiter or ControlledRateLimiter()
    controlled_adapter = adapter or FakeEmailDeliveryAdapter(capture_deliveries=True)
    return (
        RegistrationApplicationService(
            controlled_registration,  # type: ignore[arg-type]
            controlled_limiter,
            RateLimitPolicy("auth:register:ip", 5, 60),
            RateLimitPolicy("auth:register:email", 5, 60),
            controlled_adapter,
            structured_logger,
        ),
        controlled_registration,
        controlled_limiter,
        controlled_adapter,
    )


def _client(service: RegistrationApplicationService) -> TestClient:
    app.dependency_overrides[get_registration_application_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def test_register_happy_path_is_uniform_no_store_and_delivers_only_after_registration() -> None:
    service, registration, limiter, adapter = _service()

    response = _client(service).post(
        "/auth/register",
        json={"email": " Register@Example.com ", "password": "valid password"},
        headers={"X-Request-ID": "register-correlation"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "registration_received"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["X-Request-ID"] == "register-correlation"
    assert registration.calls == [("Register@Example.com", "valid password")]
    assert [policy.namespace for policy, _ in limiter.calls] == ["auth:register:ip", "auth:register:email"]
    assert limiter.calls[1][1] == "register@example.com"
    assert adapter.call_count == 1
    assert adapter.deliveries[0].correlation_id == "register-correlation"
    assert "test-only-raw-verification-token" not in response.text
    for forbidden in ("user_id", "workspace_id", "token", "hash", "pending_verification"):
        assert forbidden not in response.text


def test_register_delivery_is_invoked_only_after_the_registration_service_returns() -> None:
    events: list[str] = []

    class OrderedAdapter(FakeEmailDeliveryAdapter):
        def send_email_verification(self, recipient: str, raw_token: str, correlation_id: str | None) -> None:
            events.append("delivery")
            super().send_email_verification(recipient, raw_token, correlation_id)

    registration = ControlledRegistrationService(events=events)
    service, _, _, adapter = _service(registration, adapter=OrderedAdapter(capture_deliveries=True))
    response = _client(service).post("/auth/register", json={"email": "ordered@example.com", "password": "valid password"})

    assert response.status_code == 202
    assert events == ["registration_committed", "delivery"]
    assert adapter.call_count == 1


@pytest.mark.parametrize("outcome", ["created", "reissued", "hidden"])
def test_register_new_pending_and_existing_states_are_non_enumerable(outcome: str) -> None:
    service, registration, _, adapter = _service(ControlledRegistrationService(outcome))
    response = _client(service).post("/auth/register", json={"email": "person@example.com", "password": "valid password"})

    assert response.status_code == 202
    assert response.json() == {"status": "registration_received"}
    assert len(registration.calls) == 1
    assert adapter.call_count == (0 if outcome == "hidden" else 1)


@pytest.mark.parametrize("state", ["active", "locked", "suspended", "anonymized"])
def test_register_account_states_share_the_same_sanitized_acknowledgement(state: str) -> None:
    service, _, _, adapter = _service(ControlledRegistrationService("hidden"))
    response = _client(service).post("/auth/register", json={"email": f"{state}@example.com", "password": "valid password"})

    assert response.status_code == 202
    assert response.json() == {"status": "registration_received"}
    assert adapter.call_count == 0


def test_register_rate_limit_happens_before_registration_and_has_retry_after() -> None:
    service, registration, limiter, adapter = _service(limiter=ControlledRateLimiter(allowed=False))
    response = _client(service).post("/auth/register", json={"email": "limited@example.com", "password": "valid password"})

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many registration attempts"}
    assert response.headers["Retry-After"] == "42"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(limiter.calls) == 1
    assert registration.calls == []
    assert adapter.call_count == 0


def test_register_failure_before_commit_is_sanitized_and_never_delivers() -> None:
    service, registration, _, adapter = _service(ControlledRegistrationService("failure"))
    response = _client(service).post("/auth/register", json={"email": "failure@example.com", "password": "valid password"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Registration service unavailable"}
    assert response.headers["Cache-Control"] == "no-store"
    assert len(registration.calls) == 1
    assert adapter.call_count == 0
    assert "controlled registration failure" not in response.text


def test_register_delivery_failure_is_post_commit_uniform_and_never_leaks_token() -> None:
    adapter = FakeEmailDeliveryAdapter(capture_deliveries=True, fail=True)
    service, registration, _, _ = _service(adapter=adapter)
    response = _client(service).post("/auth/register", json={"email": "delivery@example.com", "password": "valid password"})

    assert response.status_code == 202
    assert response.json() == {"status": "registration_received"}
    assert len(registration.calls) == 1
    assert adapter.call_count == 1 and adapter.deliveries == []
    assert "test-only-raw-verification-token" not in response.text


def test_register_delivery_failure_logs_only_sanitized_operational_metadata() -> None:
    stream = StringIO()
    logger = logging.getLogger(f"registration-delivery-{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredLogFormatter())
    logger.addHandler(handler)
    adapter = FakeEmailDeliveryAdapter(fail=True)
    service, _, _, _ = _service(adapter=adapter, structured_logger=SafeStructuredLogger(logger))
    try:
        response = _client(service).post(
            "/auth/register",
            json={"email": "private.person@example.com", "password": "private-password"},
        )
    finally:
        logger.handlers.clear()

    rendered = stream.getvalue()
    assert response.status_code == 202
    assert "registration_delivery_failed" in rendered
    for forbidden in ("private.person@example.com", "private-password", "test-only-raw-verification-token"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": 3, "password": "valid password"},
        {"email": "invalid", "password": "valid password"},
        {"email": "person@example.com", "password": "short"},
        {"email": "person@example.com", "password": "valid password", "workspace_id": str(uuid4())},
        {"email": "person@example.com", "password": "valid password", "status": "active"},
    ],
)
def test_register_http_contract_rejects_invalid_and_internal_fields(payload: dict[str, object]) -> None:
    service, registration, _, adapter = _service()
    response = _client(service).post("/auth/register", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid registration request"}
    assert "valid password" not in response.text and "short" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert registration.calls == []
    assert adapter.call_count == 0

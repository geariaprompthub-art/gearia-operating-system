"""HTTP contract for public email verification without session creation."""

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_dependencies import get_email_verification_application_service
from app.services.email_verification_application_service import (
    EmailVerificationRateLimitedError,
    EmailVerificationUnavailableError,
    EmailVerificationResult,
)


@dataclass
class ControlledVerificationService:
    outcome: str = "success"
    calls: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.calls = [] if self.calls is None else self.calls

    def confirm(self, token: str, client_ip: str) -> EmailVerificationResult:
        self.calls.append((token, client_ip))
        if self.outcome == "limited":
            raise EmailVerificationRateLimitedError(24)
        if self.outcome == "failure":
            raise EmailVerificationUnavailableError("controlled failure")
        return EmailVerificationResult(verified_now=self.outcome == "success")


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    original = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


def _client(service: ControlledVerificationService) -> TestClient:
    app.dependency_overrides[get_email_verification_application_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("outcome", ["success", "already_used"])
def test_confirm_http_is_uniform_no_store_and_creates_no_cookies(outcome: str) -> None:
    service = ControlledVerificationService(outcome)
    response = _client(service).post(
        "/auth/email-verification/confirm",
        json={"token": "test-only-opaque-token"},
        headers={"X-Request-ID": "verification-correlation"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "email_verified"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["X-Request-ID"] == "verification-correlation"
    assert response.headers.get_list("set-cookie") == []
    assert service.calls == [("test-only-opaque-token", "testclient")]
    for forbidden in ("user_id", "workspace_id", "token", "hash", "session", "jwt", "refresh"):
        assert forbidden not in response.text


def test_confirm_http_rate_limit_and_operational_error_are_sanitized() -> None:
    limited = _client(ControlledVerificationService("limited")).post(
        "/auth/email-verification/confirm", json={"token": "rate-token"}
    )
    failed = _client(ControlledVerificationService("failure")).post(
        "/auth/email-verification/confirm", json={"token": "private-token"}
    )

    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many email verification attempts"}
    assert limited.headers["Retry-After"] == "24"
    assert failed.status_code == 500
    assert failed.json() == {"detail": "Email verification service unavailable"}
    assert "controlled failure" not in failed.text and "private-token" not in failed.text


@pytest.mark.parametrize("payload", [{}, {"token": 1}, {"token": ""}, {"token": "opaque", "user_id": "forbidden"}])
def test_confirm_http_rejects_invalid_payload_without_echoing_token(payload: dict[str, object]) -> None:
    service = ControlledVerificationService()
    response = _client(service).post("/auth/email-verification/confirm", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid email verification request"}
    assert response.headers["Cache-Control"] == "no-store"
    assert service.calls == []

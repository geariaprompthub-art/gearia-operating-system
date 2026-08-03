"""HTTP contracts for anonymous password-reset endpoints."""

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_dependencies import get_password_reset_application_service
from app.services.password_reset_application_service import (
    PasswordResetConfirmResult,
    PasswordResetRateLimitedError,
    PasswordResetRequestResult,
    PasswordResetUnavailableError,
)


@dataclass
class ControlledPasswordResetService:
    outcome: str = "success"
    request_calls: list[tuple[str, str, str | None]] | None = None
    confirm_calls: list[tuple[str, str, str]] | None = None

    def __post_init__(self) -> None:
        self.request_calls = [] if self.request_calls is None else self.request_calls
        self.confirm_calls = [] if self.confirm_calls is None else self.confirm_calls

    def request(self, email: str, client_ip: str, correlation_id: str | None) -> PasswordResetRequestResult:
        self.request_calls.append((email, client_ip, correlation_id))
        if self.outcome == "limited":
            raise PasswordResetRateLimitedError(25)
        if self.outcome == "failure":
            raise PasswordResetUnavailableError("controlled request failure")
        return PasswordResetRequestResult(self.outcome == "success", False)

    def confirm(self, token: str, password: str, client_ip: str) -> PasswordResetConfirmResult:
        self.confirm_calls.append((token, password, client_ip))
        if self.outcome == "limited":
            raise PasswordResetRateLimitedError(25)
        if self.outcome == "failure":
            raise PasswordResetUnavailableError("controlled confirmation failure")
        return PasswordResetConfirmResult(self.outcome == "success")


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    original = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


def _client(service: ControlledPasswordResetService) -> TestClient:
    app.dependency_overrides[get_password_reset_application_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("outcome", ["success", "hidden"])
def test_request_http_is_uniform_no_store_and_does_not_create_authentication(outcome: str) -> None:
    service = ControlledPasswordResetService(outcome)
    response = _client(service).post(
        "/auth/password-reset/request",
        json={"email": " Reset@Example.com "},
        headers={"X-Request-ID": "reset-request-correlation"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "password_reset_requested"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"] == "reset-request-correlation"
    assert response.headers.get_list("set-cookie") == []
    assert service.request_calls == [("Reset@Example.com", "testclient", "reset-request-correlation")]
    for forbidden in ("user_id", "token", "hash", "session", "jwt", "refresh", "workspace"):
        assert forbidden not in response.text


@pytest.mark.parametrize("outcome", ["success", "hidden"])
def test_confirm_http_is_uniform_no_store_and_does_not_create_authentication(outcome: str) -> None:
    service = ControlledPasswordResetService(outcome)
    response = _client(service).post(
        "/auth/password-reset/confirm",
        json={"token": "test-only-opaque-token", "password": "new valid password"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "password_reset_completed"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers.get_list("set-cookie") == []
    for forbidden in ("user_id", "token", "hash", "session", "jwt", "refresh", "workspace"):
        assert forbidden not in response.text


def test_password_reset_http_rate_limit_and_operational_failure_are_sanitized() -> None:
    limited = _client(ControlledPasswordResetService("limited")).post(
        "/auth/password-reset/request", json={"email": "limited@example.com"}
    )
    failed = _client(ControlledPasswordResetService("failure")).post(
        "/auth/password-reset/confirm",
        json={"token": "private-token", "password": "new valid password"},
    )

    assert limited.status_code == 429 and limited.headers["Retry-After"] == "25"
    assert limited.json() == {"detail": "Too many password reset attempts"}
    assert failed.status_code == 500
    assert failed.json() == {"detail": "Password reset service unavailable"}
    assert "private-token" not in failed.text and "controlled confirmation failure" not in failed.text


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/auth/password-reset/request", {}),
        ("/auth/password-reset/request", {"email": "invalid"}),
        ("/auth/password-reset/request", {"email": "person@example.com", "status": "active"}),
        ("/auth/password-reset/confirm", {"token": 1, "password": "new valid password"}),
        ("/auth/password-reset/confirm", {"token": "private-token", "password": "short"}),
        ("/auth/password-reset/confirm", {"token": "private-token", "password": "new valid password", "user_id": "forbidden"}),
    ],
)
def test_password_reset_http_rejects_invalid_payload_without_echoing_sensitive_input(path: str, payload: dict[str, object]) -> None:
    service = ControlledPasswordResetService()
    response = _client(service).post(path, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid password reset request"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-token" not in response.text and "new valid password" not in response.text
    assert service.request_calls == [] and service.confirm_calls == []

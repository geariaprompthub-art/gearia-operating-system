"""HTTP contract for authenticated, irreversible account deletion."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.account_anonymization_application_service import (
    AccountAnonymizationCsrfError,
    AccountAnonymizationRateLimitedError,
    AccountAnonymizationResult,
    AccountAnonymizationUnavailableError,
)
from app.services.auth_dependencies import (
    get_account_anonymization_application_service,
    get_cookie_policy,
)
from app.services.cookie_policy import CookiePolicy
from app.services.principal_dependencies import get_current_principal


def _principal() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(uuid4(), uuid4(), 1, uuid4(), now, now + timedelta(minutes=5), "person@example.com", "active", now, now)


@dataclass
class ControlledService:
    outcome: str = "success"
    calls: int = 0

    def anonymize_account(self, *args: object) -> AccountAnonymizationResult:
        self.calls += 1
        if self.outcome == "csrf":
            raise AccountAnonymizationCsrfError("private csrf")
        if self.outcome == "limited":
            raise AccountAnonymizationRateLimitedError(17)
        if self.outcome == "failure":
            raise AccountAnonymizationUnavailableError("private database failure")
        return AccountAnonymizationResult(True)


@pytest.fixture(autouse=True)
def overrides() -> None:
    original = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[get_cookie_policy] = lambda: CookiePolicy(False, "lax", None, 60, 60)
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


def _client(service: ControlledService) -> TestClient:
    app.dependency_overrides[get_account_anonymization_application_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def test_delete_me_is_empty_no_store_and_clears_all_authentication_cookies() -> None:
    service = ControlledService()
    response = _client(service).request(
        "DELETE", "/auth/me", json={"confirmation": "DELETE"}, headers={"X-CSRF-Token": "csrf"}
    )
    rendered = "\n".join(response.headers.get_list("set-cookie"))
    assert response.status_code == 204 and response.content == b"" and service.calls == 1
    assert response.headers["Cache-Control"] == "no-store"
    assert all(name in rendered for name in ("gearia_access=", "gearia_refresh=", "gearia_csrf="))
    assert all("Max-Age=0" in value for value in response.headers.get_list("set-cookie"))
    for forbidden in ("user", "workspace", "email", "token", "hash", "password"):
        assert forbidden not in response.text.lower()


@pytest.mark.parametrize("payload", [{}, {"confirmation": "delete"}, {"confirmation": "DELETE", "email": "private@example.com"}])
def test_delete_me_requires_strict_confirmation_without_echoing_payload(payload: dict[str, object]) -> None:
    service = ControlledService()
    response = _client(service).request("DELETE", "/auth/me", json=payload)
    assert response.status_code == 422 and response.json() == {"detail": "Invalid account deletion request"}
    assert response.headers["Cache-Control"] == "no-store" and service.calls == 0
    assert "private@example.com" not in response.text


@pytest.mark.parametrize("outcome,status_code,detail", [
    ("csrf", 403, "Account deletion failed"),
    ("limited", 429, "Too many account deletion attempts"),
    ("failure", 500, "Account deletion unavailable"),
])
def test_delete_me_errors_are_sanitized_and_never_clear_cookies(outcome: str, status_code: int, detail: str) -> None:
    response = _client(ControlledService(outcome)).request("DELETE", "/auth/me", json={"confirmation": "DELETE"})
    assert response.status_code == status_code and response.json() == {"detail": detail}
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers and "private" not in response.text

"""Regression coverage for the P0 operational security foundation."""

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core import health
from app.main import app


def test_production_settings_reject_insecure_debug_and_default_database() -> None:
    """Production settings fail early for unsafe settings that are locally convenient."""

    try:
        Settings(
            environment="production",
            debug=True,
            database_url="postgresql+psycopg://gearia:gearia@postgres:5432/gearia",
            trusted_hosts=["api.gearia.com.br"],
            cors_origins=["https://app.gearia.com.br"],
            lifecycle_token_pepper="test-only-lifecycle-pepper",
        )
    except ValidationError as error:
        message = str(error)
    else:
        raise AssertionError("insecure production settings must be rejected")

    assert "debug must be disabled" in message


def test_production_settings_accept_restricted_hosts_and_cors() -> None:
    """A production configuration with explicit safe values remains supported."""

    settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://app-user:strong-password@postgres:5432/gearia",
        trusted_hosts=["api.gearia.com.br"],
        cors_origins=["https://app.gearia.com.br"],
        lifecycle_token_pepper="test-only-lifecycle-pepper",
    )

    assert settings.debug is False
    assert settings.trusted_hosts == ["api.gearia.com.br"]


def test_health_live_is_dependency_free_and_security_headers_are_present() -> None:
    """Liveness remains cheap while every HTTP response receives safe headers."""

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"


def test_health_ready_reports_only_sanitized_dependency_statuses(monkeypatch: object) -> None:
    """Readiness reports local dependency status without leaking connection details."""

    monkeypatch.setattr(health, "check_postgres", lambda _: True)
    monkeypatch.setattr(health, "check_redis", lambda _: False)

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"postgres": "ok", "redis": "unavailable"},
    }


def test_health_ready_returns_ok_when_local_dependencies_are_healthy(monkeypatch: object) -> None:
    """Readiness is successful only when every required dependency responds."""

    monkeypatch.setattr(health, "check_postgres", lambda _: True)
    monkeypatch.setattr(health, "check_redis", lambda _: True)

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"postgres": "ok", "redis": "ok"},
    }

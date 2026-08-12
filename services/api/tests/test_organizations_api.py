"""HTTP contract tests for the four P3A organizations endpoints."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import (
    get_organization_metadata_application_service,
    get_organization_read_application_service,
    get_shared_organization_application_service,
    require_organization_create_rate_limit,
    require_organization_update_rate_limit,
)
from app.services.organization_metadata_application_service import OrganizationMetadataError, OrganizationMetadataResult
from app.services.organization_read_application_service import OrganizationRead, OrganizationReadUnavailableError
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf
from app.services.shared_organization_application_service import SharedOrganizationResult, SharedOrganizationSlugConflictError


NOW = datetime.now(UTC)
ACTOR = uuid4()
ORG = uuid4()


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(ACTOR, uuid4(), 1, uuid4(), NOW, NOW, "private@example.test", "active", NOW, NOW)


class ReadService:
    def __init__(self, outcome: str = "ok") -> None: self.outcome = outcome
    def list_accessible(self, _: UUID) -> list[OrganizationRead]: return [self._item()]
    def get_accessible(self, _: UUID, __: UUID) -> OrganizationRead:
        if self.outcome == "missing": raise OrganizationReadUnavailableError("private")
        if self.outcome == "unexpected": raise RuntimeError("private database failure")
        return self._item()
    @staticmethod
    def _item() -> OrganizationRead: return OrganizationRead(ORG, "shared", "Acme", "acme", "active", NOW, NOW)


class CreateService:
    def __init__(self, outcome: str = "ok") -> None: self.outcome = outcome; self.calls: list[tuple[UUID, str, str]] = []
    def create(self, actor: UUID, name: str, slug: str) -> SharedOrganizationResult:
        self.calls.append((actor, name, slug))
        if self.outcome == "conflict": raise SharedOrganizationSlugConflictError("private constraint")
        if self.outcome == "unexpected": raise RuntimeError("private database failure")
        return SharedOrganizationResult(ORG, uuid4(), uuid4(), name.strip(), slug, NOW, NOW)


class MetadataService:
    def __init__(self, outcome: str = "ok") -> None: self.outcome = outcome; self.calls: list[tuple[UUID, UUID, str]] = []
    def update_name(self, actor: UUID, organization: UUID, name: str) -> OrganizationMetadataResult:
        self.calls.append((actor, organization, name))
        if self.outcome == "denied": raise OrganizationMetadataError("private")
        return OrganizationMetadataResult(organization, name.strip(), "acme", "shared", "active", NOW, NOW)


@pytest.fixture(autouse=True)
def overrides() -> None:
    original = dict(app.dependency_overrides); app.dependency_overrides.clear()
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    app.dependency_overrides[require_organization_create_rate_limit] = lambda: None
    app.dependency_overrides[require_organization_update_rate_limit] = lambda: None
    try: yield
    finally: app.dependency_overrides.clear(); app.dependency_overrides.update(original)


def _client(read: ReadService | None = None, create: CreateService | None = None, metadata: MetadataService | None = None) -> TestClient:
    app.dependency_overrides[get_organization_read_application_service] = lambda: read or ReadService()
    app.dependency_overrides[get_shared_organization_application_service] = lambda: create or CreateService()
    app.dependency_overrides[get_organization_metadata_application_service] = lambda: metadata or MetadataService()
    return TestClient(app, raise_server_exceptions=False)


def test_list_and_get_are_private_authenticated_reads_without_csrf() -> None:
    client = _client()
    listed, detail = client.get("/organizations"), client.get(f"/organizations/{ORG}")
    assert listed.status_code == detail.status_code == 200
    assert listed.json()[0]["id"] == str(ORG) and detail.json()["slug"] == "acme"
    assert listed.headers["Cache-Control"] == detail.headers["Cache-Control"] == "no-store"
    for body in (listed.text, detail.text):
        for forbidden in ("personal_owner_user_id", "owner_user_id", "membership", "token", "hash"):
            assert forbidden not in body


def test_create_is_strict_csrf_wired_and_returns_only_committed_service_ids() -> None:
    service = CreateService(); response = _client(create=service).post("/organizations", json={"name": " Acme ", "slug": "acme"})
    assert response.status_code == 201 and response.headers["Cache-Control"] == "no-store"
    assert service.calls == [(ACTOR, " Acme ", "acme")]
    assert response.json()["organization"] == {"id": str(ORG), "kind": "shared", "name": "Acme", "slug": "acme", "status": "active", "created_at": NOW.isoformat().replace("+00:00", "Z"), "updated_at": NOW.isoformat().replace("+00:00", "Z")}
    assert _client().post("/organizations", json={"name": "Acme", "slug": "acme", "kind": "shared"}).status_code == 422


def test_create_conflict_rate_limit_and_unexpected_errors_are_sanitized() -> None:
    assert _client(create=CreateService("conflict")).post("/organizations", json={"name": "Acme", "slug": "acme"}).status_code == 409
    app.dependency_overrides[require_organization_create_rate_limit] = lambda: (_ for _ in ()).throw(HTTPException(429, "Too many organization requests", headers={"Retry-After": "9"}))
    limited = _client().post("/organizations", json={"name": "Acme", "slug": "acme"})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "9"
    app.dependency_overrides[require_organization_create_rate_limit] = lambda: None
    failed = _client(create=CreateService("unexpected")).post("/organizations", json={"name": "Acme", "slug": "acme"})
    assert failed.status_code == 500 and "private" not in failed.text


def test_get_idor_and_nonexistent_are_indistinguishable() -> None:
    client = _client(read=ReadService("missing")); missing = client.get(f"/organizations/{uuid4()}"); foreign = client.get(f"/organizations/{ORG}")
    assert missing.status_code == foreign.status_code == 404
    assert missing.json() == foreign.json() == {"detail": "Organization not found"}


def test_patch_is_name_only_and_policy_denial_does_not_write() -> None:
    service = MetadataService(); updated = _client(metadata=service).patch(f"/organizations/{ORG}", json={"name": " Updated "})
    assert updated.status_code == 200 and updated.json()["name"] == "Updated" and updated.json()["slug"] == "acme"
    assert service.calls == [(ACTOR, ORG, " Updated ")] and updated.headers["Cache-Control"] == "no-store"
    for payload in ({"slug": "changed"}, {"kind": "shared"}, {"status": "blocked"}, {"name": ""}):
        assert _client(metadata=service).patch(f"/organizations/{ORG}", json=payload).status_code == 422
    denied_service = MetadataService("denied"); denied = _client(metadata=denied_service).patch(f"/organizations/{ORG}", json={"name": "Denied"})
    assert denied.status_code == 403 and denied_service.calls == [(ACTOR, ORG, "Denied")]


def test_openapi_contains_organization_operations() -> None:
    paths = app.openapi()["paths"]
    assert set(paths["/organizations"]) == {"get", "post"}
    assert set(paths["/organizations/{organization_id}"]) == {"get", "patch"}

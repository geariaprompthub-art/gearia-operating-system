"""One real PostgreSQL HTTP integration for the P3A organization boundary."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.models.workspace import Workspace
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import require_organization_create_rate_limit, require_organization_update_rate_limit
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf


PREFIX = "p3a-organizations-api-"


def _clear() -> None:
    with SessionLocal() as database:
        organizations = list(database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%"))))
        database.execute(delete(Workspace).where(Workspace.organization_id.in_(organizations)))
        database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organizations)))
        database.execute(delete(Organization).where(Organization.id.in_(organizations)))
        database.execute(delete(User).where(User.email_normalized.like(f"{PREFIX}%")))
        database.commit()


@pytest.fixture(autouse=True)
def database_rows_and_overrides() -> None:
    original = dict(app.dependency_overrides); app.dependency_overrides.clear(); _clear()
    try: yield
    finally: app.dependency_overrides.clear(); app.dependency_overrides.update(original); _clear()


def test_real_postgresql_http_create_list_get_and_patch_preserve_organization_invariants() -> None:
    with SessionLocal() as database:
        user = User(email=f"{PREFIX}{uuid4().hex}@example.test", email_normalized=f"{PREFIX}{uuid4().hex}@example.test", password_hash="test", status="active")
        user.email_normalized = user.email
        database.add(user); database.commit(); user_id = user.id
    now = datetime.now(UTC)
    principal = AuthenticatedPrincipal(user_id, uuid4(), 1, uuid4(), now, now + timedelta(minutes=5), "private@example.test", "active", now, now)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    app.dependency_overrides[require_organization_create_rate_limit] = lambda: None
    app.dependency_overrides[require_organization_update_rate_limit] = lambda: None
    slug = f"{PREFIX}{uuid4().hex}"
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/organizations", json={"name": " Team ", "slug": slug})
    assert created.status_code == 201 and created.json()["organization"]["kind"] == "shared"
    organization_id = UUID(created.json()["organization"]["id"])
    assert client.get("/organizations").json()[0]["id"] == str(organization_id)
    assert client.get(f"/organizations/{organization_id}").status_code == 200
    changed = client.patch(f"/organizations/{organization_id}", json={"name": "Renamed"})
    assert changed.status_code == 200 and changed.json()["slug"] == slug
    with SessionLocal() as database:
        organization = database.get(Organization, organization_id)
        workspace = database.scalar(select(Workspace).where(Workspace.organization_id == organization_id))
        assert organization is not None and organization.name == "Renamed" and organization.slug == slug
        assert workspace is not None and workspace.owner_user_id is None

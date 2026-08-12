"""Real PostgreSQL proof for scoped membership HTTP lifecycle operations."""

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
from app.services.organization_dependencies import require_membership_revoke_rate_limit, require_membership_update_rate_limit
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf

PREFIX = "p3a-memberships-api-"


def _clear() -> None:
    with SessionLocal() as database:
        ids = list(database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%"))))
        database.execute(delete(Workspace).where(Workspace.organization_id.in_(ids)))
        database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(ids)))
        database.execute(delete(Organization).where(Organization.id.in_(ids)))
        database.execute(delete(User).where(User.email_normalized.like(f"{PREFIX}%")))
        database.commit()


@pytest.fixture(autouse=True)
def cleanup_and_overrides():
    original = dict(app.dependency_overrides); app.dependency_overrides.clear(); _clear()
    try: yield
    finally: app.dependency_overrides.clear(); app.dependency_overrides.update(original); _clear()


def _user(database, label: str) -> User:
    email = f"{PREFIX}{label}-{uuid4().hex}@example.test"; result = User(email=email, email_normalized=email, password_hash="test", status="active")
    database.add(result); database.flush(); return result


def _organization(database, owner: User, member: User, slug: str) -> tuple[Organization, OrganizationMembership, OrganizationMembership]:
    organization = Organization(kind="shared", name="Membership API", slug=slug, status="active", personal_owner_user_id=None)
    database.add(organization); database.flush()
    owner_membership = OrganizationMembership(organization_id=organization.id, user_id=owner.id, role="owner")
    member_membership = OrganizationMembership(organization_id=organization.id, user_id=member.id, role="member")
    database.add_all([owner_membership, member_membership]); database.commit(); return organization, owner_membership, member_membership


def test_real_postgresql_http_list_transition_revoke_and_cross_org_target_are_scoped() -> None:
    with SessionLocal() as database:
        owner, member, outsider = _user(database, "owner"), _user(database, "member"), _user(database, "outsider")
        first, owner_membership, member_membership = _organization(database, owner, member, f"{PREFIX}first-{uuid4().hex}")
        second, _, foreign_membership = _organization(database, outsider, _user(database, "other"), f"{PREFIX}second-{uuid4().hex}")
        owner_id, first_id, second_id, owner_membership_id, target_id, foreign_id = owner.id, first.id, second.id, owner_membership.id, member_membership.id, foreign_membership.id
    now = datetime.now(UTC)
    app.dependency_overrides[get_current_principal] = lambda: AuthenticatedPrincipal(owner_id, uuid4(), 1, uuid4(), now, now + timedelta(minutes=5), "private@example.test", "active", now, now)
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    app.dependency_overrides[require_membership_update_rate_limit] = lambda: None
    app.dependency_overrides[require_membership_revoke_rate_limit] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)
    listed = client.get(f"/organizations/{first_id}/memberships")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()] == [str(owner_membership_id), str(target_id)]
    changed = client.patch(f"/organizations/{first_id}/memberships/{target_id}", json={"role": "admin"})
    assert changed.status_code == 200 and changed.json()["role"] == "admin"
    cross = client.patch(f"/organizations/{first_id}/memberships/{foreign_id}", json={"role": "admin"})
    assert cross.status_code == 403 and "second" not in cross.text
    revoked = client.delete(f"/organizations/{first_id}/memberships/{target_id}")
    assert revoked.status_code == 204
    with SessionLocal() as database:
        row = database.get(OrganizationMembership, target_id); foreign = database.get(OrganizationMembership, foreign_id)
        assert row is not None and row.revoked_at is not None
        assert foreign is not None and foreign.revoked_at is None and foreign.organization_id == second_id

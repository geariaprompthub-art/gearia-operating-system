"""End-to-end HTTP composition and IDOR proof for all P3A 7B operations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models.organization import Organization, OrganizationInvitation, OrganizationMembership
from app.models.user import User
from app.models.workspace import Workspace
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.organization_dependencies import (
    get_organization_invitation_application_service,
    require_invitation_accept_rate_limit,
    require_invitation_issue_rate_limit,
    require_invitation_revoke_rate_limit,
    require_membership_revoke_rate_limit,
    require_membership_update_rate_limit,
    require_organization_create_rate_limit,
    require_organization_update_rate_limit,
)
from app.services.organization_invitation_application_service import (
    OrganizationInvitationApplicationService,
)
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf


PREFIX = "p3a-http-flow-"
PEPPER = "test-only-p3a-http-flow-pepper"


def _clear() -> None:
    with SessionLocal() as database:
        organization_ids = list(database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%"))))
        database.execute(delete(Workspace).where(Workspace.organization_id.in_(organization_ids)))
        database.execute(delete(OrganizationInvitation).where(OrganizationInvitation.organization_id.in_(organization_ids)))
        database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organization_ids)))
        database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
        database.execute(delete(User).where(User.email_normalized.like(f"{PREFIX}%")))
        database.commit()


def _principal(user_id: UUID) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(user_id, uuid4(), 1, uuid4(), now, now + timedelta(minutes=5), "private@example.test", "active", now, now)


@pytest.fixture(autouse=True)
def clean_database_and_overrides():
    original = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    _clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)
        _clear()


def test_all_eleven_operations_compose_and_cross_tenant_ids_fail_closed() -> None:
    with SessionLocal() as database:
        users: list[User] = []
        for label in ("owner", "invitee", "outsider"):
            email = f"{PREFIX}{label}-{uuid4().hex}@example.test"
            users.append(User(email=email, email_normalized=email, password_hash="test", status="active"))
        database.add_all(users)
        database.flush()
        foreign_organization = Organization(kind="shared", name="Foreign", slug=f"{PREFIX}foreign-{uuid4().hex}", status="active", personal_owner_user_id=None)
        database.add(foreign_organization)
        database.flush()
        foreign_owner = OrganizationMembership(organization_id=foreign_organization.id, user_id=users[2].id, role="owner")
        database.add(foreign_owner)
        database.flush()
        foreign_invitation = OrganizationInvitation(organization_id=foreign_organization.id, invited_email_normalized=users[2].email_normalized, role="member", token_hash="test-only-foreign-hash", expires_at=datetime.now(UTC) + timedelta(days=1), created_by_membership_id=foreign_owner.id)
        database.add(foreign_invitation)
        database.commit()
        owner_id, invitee_id, foreign_id, foreign_invitation_id = users[0].id, users[1].id, foreign_organization.id, foreign_invitation.id
        invitee_email, outsider_email = users[1].email, users[2].email

    current = {"user_id": owner_id}
    invitation_database = SessionLocal()
    adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
    invitation_service = OrganizationInvitationApplicationService(invitation_database, PEPPER, adapter)
    app.dependency_overrides[get_current_principal] = lambda: _principal(current["user_id"])
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    for dependency in (
        require_organization_create_rate_limit,
        require_organization_update_rate_limit,
        require_membership_update_rate_limit,
        require_membership_revoke_rate_limit,
        require_invitation_issue_rate_limit,
        require_invitation_revoke_rate_limit,
        require_invitation_accept_rate_limit,
    ):
        app.dependency_overrides[dependency] = lambda: None
    app.dependency_overrides[get_organization_invitation_application_service] = lambda: invitation_service

    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/organizations", json={"name": "Flow Team", "slug": f"{PREFIX}flow-{uuid4().hex}"})
    assert created.status_code == 201
    organization_id = UUID(created.json()["organization"]["id"])
    assert client.get("/organizations").status_code == 200
    assert client.get(f"/organizations/{organization_id}").status_code == 200
    assert client.patch(f"/organizations/{organization_id}", json={"name": "Flow Renamed"}).status_code == 200
    memberships = client.get(f"/organizations/{organization_id}/memberships")
    assert memberships.status_code == 200 and len(memberships.json()) == 1

    issued = client.post(f"/organizations/{organization_id}/invitations", json={"email": invitee_email, "role": "member"})
    assert issued.status_code == 202 and adapter.call_count == 1
    token = adapter.deliveries[0].raw_token
    invitations = client.get(f"/organizations/{organization_id}/invitations")
    assert invitations.status_code == 200 and len(invitations.json()) == 1 and token not in invitations.text

    foreign_read = client.get(f"/organizations/{foreign_id}")
    foreign_update = client.patch(f"/organizations/{foreign_id}", json={"name": "Nope"})
    assert foreign_read.status_code == 404
    assert foreign_update.status_code == 403
    assert "Foreign" not in foreign_read.text and "Foreign" not in foreign_update.text
    assert client.delete(f"/organizations/{organization_id}/invitations/{foreign_invitation_id}").status_code == 403

    current["user_id"] = invitee_id
    assert client.post("/organization-invitations/accept", json={"token": token}).status_code == 200
    current["user_id"] = owner_id
    memberships = client.get(f"/organizations/{organization_id}/memberships")
    invitee_membership_id = UUID(next(item["id"] for item in memberships.json() if item["user_id"] == str(invitee_id)))
    assert client.patch(f"/organizations/{organization_id}/memberships/{invitee_membership_id}", json={"role": "admin"}).status_code == 200
    second = client.post(f"/organizations/{organization_id}/invitations", json={"email": outsider_email, "role": "member"})
    assert second.status_code == 202
    with SessionLocal() as database:
        second_invitation_id = database.scalar(
            select(OrganizationInvitation.id).where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.invalidated_at.is_(None),
            )
        )
    assert second_invitation_id is not None
    assert client.delete(f"/organizations/{organization_id}/invitations/{second_invitation_id}").status_code == 204
    assert client.delete(f"/organizations/{organization_id}/memberships/{invitee_membership_id}").status_code == 204

    with SessionLocal() as database:
        foreign_invitation = database.get(OrganizationInvitation, foreign_invitation_id)
        membership = database.get(OrganizationMembership, invitee_membership_id)
        organization = database.get(Organization, organization_id)
        assert foreign_invitation is not None and foreign_invitation.invalidated_at is None
        assert membership is not None and membership.revoked_at is not None
        assert organization is not None and organization.name == "Flow Renamed"
    invitation_database.close()

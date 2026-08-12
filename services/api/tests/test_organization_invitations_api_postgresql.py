"""Real PostgreSQL HTTP proof for P3A invitation issue, binding, and acceptance."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
)
from app.services.organization_invitation_application_service import OrganizationInvitationApplicationService
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf

PREFIX = "p3a-invitations-api-"; PEPPER = "test-only-p3a-invitations-pepper"


def _clear() -> None:
    with SessionLocal() as database:
        ids = list(database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%"))))
        database.execute(delete(Workspace).where(Workspace.organization_id.in_(ids)))
        database.execute(delete(OrganizationInvitation).where(OrganizationInvitation.organization_id.in_(ids)))
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
    email = f"{PREFIX}{label}-{uuid4().hex}@example.test"; user = User(email=email, email_normalized=email, password_hash="test", status="active")
    database.add(user); database.flush(); return user


def _principal(user_id):
    now = datetime.now(UTC); return AuthenticatedPrincipal(user_id, uuid4(), 1, uuid4(), now, now + timedelta(minutes=5), "private@example.test", "active", now, now)


def test_real_postgresql_http_issue_accept_email_binding_and_historical_token_safety() -> None:
    with SessionLocal() as database:
        owner, invitee, other = _user(database, "owner"), _user(database, "invitee"), _user(database, "other")
        organization = Organization(kind="shared", name="Invitations", slug=f"{PREFIX}{uuid4().hex}", status="active", personal_owner_user_id=None)
        database.add(organization); database.flush(); database.add(OrganizationMembership(organization_id=organization.id, user_id=owner.id, role="owner")); database.commit()
        owner_id, invitee_id, other_id, organization_id, invitee_email = owner.id, invitee.id, other.id, organization.id, invitee.email
    database = SessionLocal(); delivery = FakeEmailDeliveryAdapter(capture_deliveries=True); service = OrganizationInvitationApplicationService(database, PEPPER, delivery)
    app.dependency_overrides[get_organization_invitation_application_service] = lambda: service
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    app.dependency_overrides[require_invitation_issue_rate_limit] = lambda: None
    app.dependency_overrides[require_invitation_revoke_rate_limit] = lambda: None
    app.dependency_overrides[require_invitation_accept_rate_limit] = lambda: None
    app.dependency_overrides[get_current_principal] = lambda: _principal(owner_id)
    client = TestClient(app, raise_server_exceptions=False)
    issued = client.post(f"/organizations/{organization_id}/invitations", json={"email": invitee_email, "role": "admin"})
    assert issued.status_code == 202 and issued.json() == {"status": "invitation_created"} and delivery.call_count == 1
    raw = delivery.deliveries[0].raw_token
    assert raw not in issued.text
    with SessionLocal() as verification:
        invitation = verification.scalar(select(OrganizationInvitation).where(OrganizationInvitation.organization_id == organization_id))
        assert invitation is not None and invitation.token_hash != raw and invitation.accepted_at is None
    app.dependency_overrides[get_current_principal] = lambda: _principal(other_id)
    mismatch = client.post("/organization-invitations/accept", json={"token": raw})
    assert mismatch.status_code == 403 and raw not in mismatch.text
    app.dependency_overrides[get_current_principal] = lambda: _principal(invitee_id)
    accepted = client.post("/organization-invitations/accept", json={"token": raw})
    assert accepted.status_code == 200 and accepted.json() == {"status": "invitation_processed"}
    replay = client.post("/organization-invitations/accept", json={"token": raw})
    assert replay.status_code == 403
    with SessionLocal() as verification:
        invitation = verification.scalar(select(OrganizationInvitation).where(OrganizationInvitation.organization_id == organization_id))
        membership = verification.scalar(select(OrganizationMembership).where(OrganizationMembership.organization_id == organization_id, OrganizationMembership.user_id == invitee_id, OrganizationMembership.revoked_at.is_(None)))
        assert invitation is not None and invitation.accepted_at is not None and membership is not None and membership.role == "admin"
    database.close()

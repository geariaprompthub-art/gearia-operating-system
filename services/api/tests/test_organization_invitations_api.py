"""HTTP contracts for the four P3A organization invitation endpoints."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import (
    get_organization_invitation_application_service,
    get_organization_invitation_read_application_service,
    require_invitation_accept_rate_limit,
    require_invitation_issue_rate_limit,
    require_invitation_revoke_rate_limit,
)
from app.services.organization_invitation_application_service import OrganizationInvitationAlreadyMemberError, OrganizationInvitationError
from app.services.organization_invitation_read_application_service import OrganizationInvitationRead
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf

NOW = datetime.now(UTC); ACTOR = uuid4(); ORGANIZATION = uuid4(); INVITATION = uuid4()


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(ACTOR, uuid4(), 1, uuid4(), NOW, NOW + timedelta(minutes=5), "private@example.test", "active", NOW, NOW)


class ReadService:
    def __init__(self, outcome: str = "ok"): self.outcome, self.calls = outcome, []
    def list_active(self, actor: UUID, organization: UUID):
        self.calls.append((actor, organization))
        if self.outcome == "denied": raise OrganizationInvitationError("private")
        return (OrganizationInvitationRead(INVITATION, organization, "member", NOW + timedelta(days=7), NOW),)


class InvitationService:
    def __init__(self, outcome: str = "ok"): self.outcome, self.calls = outcome, []
    def issue(self, *args):
        self.calls.append(("issue", args))
        if self.outcome == "member": raise OrganizationInvitationAlreadyMemberError("private")
    def revoke(self, *args):
        self.calls.append(("revoke", args))
        if self.outcome == "denied": raise OrganizationInvitationError("private")
    def accept(self, *args):
        self.calls.append(("accept", args))
        if self.outcome == "invalid": raise OrganizationInvitationError("private raw token")


@pytest.fixture(autouse=True)
def overrides():
    original = dict(app.dependency_overrides); app.dependency_overrides.clear()
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    app.dependency_overrides[require_invitation_issue_rate_limit] = lambda: None
    app.dependency_overrides[require_invitation_revoke_rate_limit] = lambda: None
    app.dependency_overrides[require_invitation_accept_rate_limit] = lambda: None
    try: yield
    finally: app.dependency_overrides.clear(); app.dependency_overrides.update(original)


def _client(read: ReadService | None = None, service: InvitationService | None = None) -> TestClient:
    app.dependency_overrides[get_organization_invitation_read_application_service] = lambda: read or ReadService()
    app.dependency_overrides[get_organization_invitation_application_service] = lambda: service or InvitationService()
    return TestClient(app, raise_server_exceptions=False)


def test_list_is_private_admin_projection_without_email_or_token() -> None:
    read = ReadService(); response = _client(read).get(f"/organizations/{ORGANIZATION}/invitations")
    assert response.status_code == 200 and response.json()[0]["id"] == str(INVITATION)
    assert response.headers["Cache-Control"] == "no-store" and read.calls == [(ACTOR, ORGANIZATION)]
    for forbidden in ("email", "token", "hash", "invalidated", "accepted"):
        assert forbidden not in response.text


def test_issue_is_strict_accepted_and_never_exposes_token_or_email() -> None:
    service = InvitationService(); response = _client(service=service).post(f"/organizations/{ORGANIZATION}/invitations", json={"email": "invitee@example.test", "role": "member"})
    assert response.status_code == 202 and response.json() == {"status": "invitation_created"}
    assert response.headers["Cache-Control"] == "no-store" and service.calls[0][0] == "issue"
    assert "invitee@example.test" not in response.text
    for payload in ({"email": "x@example.test", "role": "owner"}, {"email": "x@example.test", "role": "member", "token": "x"}):
        assert _client(service=service).post(f"/organizations/{ORGANIZATION}/invitations", json=payload).status_code == 422


def test_issue_existing_member_and_rate_limit_are_sanitized() -> None:
    member = _client(service=InvitationService("member")).post(f"/organizations/{ORGANIZATION}/invitations", json={"email": "x@example.test", "role": "admin"})
    assert member.status_code == 409 and "private" not in member.text
    app.dependency_overrides[require_invitation_issue_rate_limit] = lambda: (_ for _ in ()).throw(HTTPException(429, "Too many organization requests", headers={"Retry-After": "7"}))
    limited = _client().post(f"/organizations/{ORGANIZATION}/invitations", json={"email": "x@example.test", "role": "member"})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "7"


def test_revoke_and_accept_delegate_without_token_or_identity_logic_in_router() -> None:
    service = InvitationService(); revoked = _client(service=service).delete(f"/organizations/{ORGANIZATION}/invitations/{INVITATION}")
    assert revoked.status_code == 204 and service.calls[0][0] == "revoke"
    accepted = _client(service=service).post("/organization-invitations/accept", json={"token": "opaque-test-token"})
    assert accepted.status_code == 200 and accepted.json() == {"status": "invitation_processed"}
    assert "opaque-test-token" not in accepted.text and service.calls[-1] == ("accept", (ACTOR, "opaque-test-token"))
    assert _client(service=service).post("/organization-invitations/accept", json={"token": "x", "email": "x@example.test"}).status_code == 422


def test_invalid_accept_and_admin_denial_are_sanitized() -> None:
    invalid = _client(service=InvitationService("invalid")).post("/organization-invitations/accept", json={"token": "private"})
    denied = _client(read=ReadService("denied")).get(f"/organizations/{ORGANIZATION}/invitations")
    assert invalid.status_code == denied.status_code == 403
    assert "private" not in invalid.text and "private" not in denied.text


def test_openapi_contains_four_invitation_operations_without_internal_fields() -> None:
    paths = app.openapi()["paths"]
    assert set(paths["/organizations/{organization_id}/invitations"]) == {"get", "post"}
    assert set(paths["/organizations/{organization_id}/invitations/{invitation_id}"]) == {"delete"}
    assert set(paths["/organization-invitations/accept"]) == {"post"}
    rendered = str(app.openapi())
    for forbidden in ("token_hash", "personal_owner_user_id", "csrf_secret_hash"):
        assert forbidden not in rendered

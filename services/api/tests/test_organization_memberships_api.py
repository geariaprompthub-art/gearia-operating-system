"""HTTP contracts for P3A membership listing and lifecycle mutations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import (
    get_organization_membership_application_service,
    require_membership_revoke_rate_limit,
    require_membership_update_rate_limit,
)
from app.services.organization_membership_application_service import LastOrganizationOwnerError, MembershipResult, OrganizationMembershipLifecycleError
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf


NOW = datetime.now(UTC); ACTOR = uuid4(); ORGANIZATION = uuid4(); MEMBER = uuid4()


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(ACTOR, uuid4(), 1, uuid4(), NOW, NOW + timedelta(minutes=5), "private@example.test", "active", NOW, NOW)


class MembershipService:
    def __init__(self, outcome: str = "ok") -> None: self.outcome, self.calls = outcome, []
    @staticmethod
    def _result(role: str = "member") -> MembershipResult: return MembershipResult(MEMBER, ORGANIZATION, uuid4(), role, NOW, None)
    def list_active(self, actor: UUID, organization: UUID):
        self.calls.append(("list", actor, organization)); return (self._result("owner"), self._result("member"))
    def change_role(self, actor: UUID, organization: UUID, membership: UUID, role: str) -> MembershipResult:
        self.calls.append(("change", actor, organization, membership, role))
        if self.outcome == "last": raise LastOrganizationOwnerError("private")
        if self.outcome == "denied": raise OrganizationMembershipLifecycleError("private")
        return MembershipResult(membership, organization, uuid4(), role, NOW, None)
    def revoke(self, actor: UUID, organization: UUID, membership: UUID) -> MembershipResult:
        self.calls.append(("revoke", actor, organization, membership))
        if self.outcome == "last": raise LastOrganizationOwnerError("private")
        if self.outcome == "denied": raise OrganizationMembershipLifecycleError("private")
        return MembershipResult(membership, organization, uuid4(), "member", NOW, NOW)


@pytest.fixture(autouse=True)
def overrides():
    original = dict(app.dependency_overrides); app.dependency_overrides.clear()
    app.dependency_overrides[get_current_principal] = _principal
    app.dependency_overrides[require_authenticated_csrf] = lambda: None
    app.dependency_overrides[require_membership_update_rate_limit] = lambda: None
    app.dependency_overrides[require_membership_revoke_rate_limit] = lambda: None
    try: yield
    finally: app.dependency_overrides.clear(); app.dependency_overrides.update(original)


def _client(service: MembershipService) -> TestClient:
    app.dependency_overrides[get_organization_membership_application_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def test_list_returns_active_public_projection_in_service_order_without_csrf() -> None:
    service = MembershipService(); response = _client(service).get(f"/organizations/{ORGANIZATION}/memberships")
    assert response.status_code == 200 and [item["role"] for item in response.json()] == ["owner", "member"]
    assert response.headers["Cache-Control"] == "no-store" and service.calls == [("list", ACTOR, ORGANIZATION)]
    for forbidden in ("email", "revoked_at", "token", "hash"):
        assert forbidden not in response.text


def test_patch_is_strict_and_delegates_transition_without_router_policy() -> None:
    service = MembershipService(); response = _client(service).patch(f"/organizations/{ORGANIZATION}/memberships/{MEMBER}", json={"role": "admin"})
    assert response.status_code == 200 and response.json()["role"] == "admin"
    assert service.calls == [("change", ACTOR, ORGANIZATION, MEMBER, "admin")] and response.headers["Cache-Control"] == "no-store"
    for payload in ({"role": "invalid"}, {"role": "member", "user_id": str(uuid4())}, {"revoked_at": None}):
        assert _client(service).patch(f"/organizations/{ORGANIZATION}/memberships/{MEMBER}", json=payload).status_code == 422


def test_delete_soft_revocation_contract_and_rate_limit_are_sanitized() -> None:
    service = MembershipService(); response = _client(service).delete(f"/organizations/{ORGANIZATION}/memberships/{MEMBER}")
    assert response.status_code == 204 and response.content == b"" and service.calls == [("revoke", ACTOR, ORGANIZATION, MEMBER)]
    app.dependency_overrides[require_membership_revoke_rate_limit] = lambda: (_ for _ in ()).throw(HTTPException(429, "Too many organization requests", headers={"Retry-After": "8"}))
    limited = _client(MembershipService()).delete(f"/organizations/{ORGANIZATION}/memberships/{MEMBER}")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "8"


def test_last_owner_is_conflict_and_denied_or_cross_target_is_sanitized() -> None:
    last = _client(MembershipService("last")).patch(f"/organizations/{ORGANIZATION}/memberships/{MEMBER}", json={"role": "admin"})
    denied = _client(MembershipService("denied")).delete(f"/organizations/{ORGANIZATION}/memberships/{MEMBER}")
    assert last.status_code == 409 and "private" not in last.text
    assert denied.status_code == 403 and "private" not in denied.text


def test_openapi_exposes_the_three_membership_operations() -> None:
    paths = app.openapi()["paths"]
    assert set(paths["/organizations/{organization_id}/memberships"]) == {"get"}
    assert set(paths["/organizations/{organization_id}/memberships/{membership_id}"]) == {"patch", "delete"}

"""PostgreSQL contracts for organization-scoped workspace access."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.models.workspace import Workspace
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.organization_dependencies import require_organization_workspace_create_rate_limit
from app.services.principal_dependencies import get_current_principal, require_authenticated_csrf

P = "p3a-org-workspaces-"


def _principal(user_id):
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(user_id, uuid4(), 1, uuid4(), now, now + timedelta(minutes=5), "private@example.test", "active", now, now)


@pytest.fixture(autouse=True)
def cleanup():
    original = dict(app.dependency_overrides); app.dependency_overrides.clear()
    with SessionLocal() as db:
        ids = list(db.scalars(select(Organization.id).where(Organization.slug.like(f"{P}%"))))
        db.execute(delete(Workspace).where(Workspace.organization_id.in_(ids))); db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(ids))); db.execute(delete(Organization).where(Organization.id.in_(ids))); db.execute(delete(User).where(User.email_normalized.like(f"{P}%"))); db.commit()
    try: yield
    finally:
        app.dependency_overrides.clear(); app.dependency_overrides.update(original)
        with SessionLocal() as db:
            ids = list(db.scalars(select(Organization.id).where(Organization.slug.like(f"{P}%"))))
            db.execute(delete(Workspace).where(Workspace.organization_id.in_(ids))); db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(ids))); db.execute(delete(Organization).where(Organization.id.in_(ids))); db.execute(delete(User).where(User.email_normalized.like(f"{P}%"))); db.commit()


def test_shared_workspace_http_access_is_membership_scoped_and_ownerless():
    with SessionLocal() as db:
        users=[]
        for label in ("owner", "admin", "member", "outsider"):
            email=f"{P}{label}-{uuid4().hex}@example.test"; users.append(User(email=email,email_normalized=email,password_hash="test",status="active"))
        db.add_all(users); db.flush()
        org=Organization(kind="shared",name="Shared",slug=f"{P}{uuid4().hex}",status="active",personal_owner_user_id=None); other=Organization(kind="shared",name="Other",slug=f"{P}other-{uuid4().hex}",status="active",personal_owner_user_id=None); db.add_all([org,other]); db.flush()
        db.add_all([OrganizationMembership(organization_id=org.id,user_id=users[0].id,role="owner"),OrganizationMembership(organization_id=org.id,user_id=users[1].id,role="admin"),OrganizationMembership(organization_id=org.id,user_id=users[2].id,role="member"),OrganizationMembership(organization_id=other.id,user_id=users[3].id,role="owner")]); db.commit()
        ids=[user.id for user in users]; org_id,other_id=org.id,other.id
    current={"id":ids[0]}; app.dependency_overrides[get_current_principal]=lambda:_principal(current["id"]); app.dependency_overrides[require_authenticated_csrf]=lambda:None; app.dependency_overrides[require_organization_workspace_create_rate_limit]=lambda:None
    client=TestClient(app,raise_server_exceptions=False)
    created=client.post(f"/organizations/{org_id}/workspaces",json={"name":"Operations"}); assert created.status_code==201 and created.json()["organization_id"]==str(org_id)
    workspace_id=created.json()["id"]
    with SessionLocal() as db: assert db.get(Workspace,uuid4()) is None; created_row=db.get(Workspace,workspace_id); assert created_row is not None and created_row.owner_user_id is None
    current["id"]=ids[1]; assert client.get(f"/organizations/{org_id}/workspaces").status_code==200
    current["id"]=ids[2]; assert client.get(f"/organizations/{org_id}/workspaces/{workspace_id}").status_code==200; assert client.post(f"/organizations/{org_id}/workspaces",json={"name":"Denied"}).status_code==403
    current["id"]=ids[3]; assert client.get(f"/organizations/{org_id}/workspaces/{workspace_id}").status_code==403; assert client.get(f"/organizations/{other_id}/workspaces/{workspace_id}").status_code==403

"""PostgreSQL contracts for name-only organization metadata updates."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context_resolver import OrganizationContextResolver
from app.services.organization_metadata_application_service import OrganizationMetadataApplicationService, OrganizationMetadataError

P = "p3a-metadata-"

def service(db):
    organizations = OrganizationRepository(db); memberships = OrganizationMembershipRepository(db)
    return OrganizationMetadataApplicationService(db, organizations, OrganizationContextResolver(organizations, memberships))

@pytest.fixture(autouse=True)
def cleanup():
    def clear(db):
        ids = list(db.scalars(select(Organization.id).where(Organization.slug.like(f"{P}%"))))
        db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(ids)))
        db.execute(delete(Organization).where(Organization.id.in_(ids)))
        db.execute(delete(User).where(User.email_normalized.like(f"{P}%"))); db.commit()
    with SessionLocal() as db: clear(db)
    yield
    with SessionLocal() as db: clear(db)

def setup(db, kind="shared"):
    owner = User(email=f"{P}{uuid4().hex}@x.test", email_normalized=f"{P}{uuid4().hex}@x.test", password_hash="h", status="active"); db.add(owner); db.flush()
    org = Organization(kind=kind, name="Original", slug=f"{P}{uuid4().hex}", personal_owner_user_id=owner.id if kind == "personal" else None); db.add(org); db.flush(); db.add(OrganizationMembership(organization_id=org.id,user_id=owner.id,role="owner")); db.commit(); return owner,org

def test_owner_updates_only_name_and_noop_preserves_timestamp():
    with SessionLocal() as db:
        owner, org = setup(db); before = org.updated_at; result = service(db).update_name(owner.id, org.id, "  Olá Organização  ")
        db.refresh(org); assert result.name == "Olá Organização" and org.slug == result.slug and org.kind == "shared" and org.updated_at >= before
        unchanged = org.updated_at; service(db).update_name(owner.id, org.id, org.name); db.refresh(org); assert org.updated_at == unchanged

@pytest.mark.parametrize("kind", ["personal", "shared"])
def test_owner_is_authorized_and_cross_actor_is_denied(kind):
    with SessionLocal() as db:
        owner, org = setup(db, kind); outsider = User(email=f"{P}{uuid4().hex}@x.test", email_normalized=f"{P}{uuid4().hex}@x.test", password_hash="h", status="active"); db.add(outsider); db.commit()
        assert service(db).update_name(owner.id, org.id, "Updated").name == "Updated"
        with pytest.raises(OrganizationMetadataError): service(db).update_name(outsider.id, org.id, "Denied")

def test_member_and_blocked_are_denied():
    with SessionLocal() as db:
        owner, org = setup(db); member = User(email=f"{P}{uuid4().hex}@x.test", email_normalized=f"{P}{uuid4().hex}@x.test", password_hash="h", status="active"); db.add(member); db.flush(); db.add(OrganizationMembership(organization_id=org.id,user_id=member.id,role="member")); db.commit()
        with pytest.raises(OrganizationMetadataError): service(db).update_name(member.id, org.id, "Denied")
        org.status="blocked"; db.commit()
        with pytest.raises(OrganizationMetadataError): service(db).update_name(owner.id, org.id, "Denied")

@pytest.mark.parametrize("value", ["", "   ", "x" * 121])
def test_invalid_names_fail_closed(value):
    with SessionLocal() as db:
        owner, org = setup(db)
        with pytest.raises(OrganizationMetadataError): service(db).update_name(owner.id, org.id, value)

def test_rollback_after_flush_restores_name(monkeypatch):
    with SessionLocal() as db:
        owner, org = setup(db); org_id = org.id; item = service(db)
        original = item._organizations.update_name_and_slug
        def fail_after_flush(*args):
            original(*args); raise RuntimeError("injected")
        monkeypatch.setattr(item._organizations, "update_name_and_slug", fail_after_flush)
        with pytest.raises(RuntimeError): item.update_name(owner.id, org.id, "Changed")
    with SessionLocal() as check:
        row = check.get(Organization, org_id); assert row.name == "Original"

def test_two_real_sessions_serialize_name_updates():
    with SessionLocal() as db:
        owner, org = setup(db); owner_id, org_id = owner.id, org.id
    def update(value):
        with SessionLocal() as db: service(db).update_name(owner_id, org_id, value)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(update, ("Name A", "Name B")))
    with SessionLocal() as db:
        row = db.get(Organization, org_id); assert row.name in {"Name A", "Name B"} and row.slug.startswith(P)

def test_commit_failure_rolls_back_and_preserves_database(monkeypatch):
    with SessionLocal() as db:
        owner, org = setup(db); org_id = org.id; item = service(db); calls = {"commit": 0, "rollback": 0}
        def fail_commit(): calls["commit"] += 1; raise RuntimeError("commit failure")
        original_rollback = db.rollback
        def track_rollback(): calls["rollback"] += 1; original_rollback()
        monkeypatch.setattr(db, "commit", fail_commit); monkeypatch.setattr(db, "rollback", track_rollback)
        with pytest.raises(RuntimeError): item.update_name(owner.id, org_id, "Changed")
        assert calls == {"commit": 1, "rollback": 1}
    with SessionLocal() as check:
        row = check.get(Organization, org_id); assert row.name == "Original" and row.slug.startswith(P)

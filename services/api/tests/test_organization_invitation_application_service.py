"""PostgreSQL contracts for opaque shared-organization invitations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from time import sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.organization import Organization, OrganizationInvitation, OrganizationMembership
from app.models.user import User
from app.repositories.organization_invitation_repository import OrganizationInvitationRepository
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.organization_invitation_application_service import OrganizationInvitationAlreadyMemberError, OrganizationInvitationApplicationService, OrganizationInvitationError


PREFIX = "p3a-invitation-"
PEPPER = "test-only-invitation-pepper"


def clean(database):
    orgs = list(database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%"))))
    users = list(database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))))
    database.execute(delete(OrganizationInvitation).where(OrganizationInvitation.organization_id.in_(orgs)))
    database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(orgs)))
    database.execute(delete(Organization).where(Organization.id.in_(orgs)))
    database.execute(delete(User).where(User.id.in_(users))); database.commit()


@pytest.fixture(autouse=True)
def cleanup():
    with SessionLocal() as db: clean(db)
    yield
    with SessionLocal() as db: clean(db)


def user(db, label):
    email = f"{PREFIX}{label}-{uuid4().hex}@example.com"; item = User(email=email, email_normalized=email, password_hash="hash", status="active")
    db.add(item); db.flush(); return item


def shared(db, actor, role="owner", blocked=False):
    org = Organization(kind="shared", name="Invites", slug=f"{PREFIX}{uuid4().hex}", status="blocked" if blocked else "active")
    db.add(org); db.flush(); membership = OrganizationMembership(organization_id=org.id, user_id=actor.id, role=role)
    db.add(membership); db.commit(); return org, membership


def svc(db, adapter=None): return OrganizationInvitationApplicationService(db, PEPPER, adapter or FakeEmailDeliveryAdapter(capture_deliveries=True))


@pytest.mark.parametrize("role", ["member", "admin"])
def test_issue_accept_and_replay(role):
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner); adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        issued = svc(db, adapter).issue(owner.id, org.id, invitee.email, role)
        raw = adapter.deliveries[0].raw_token
        accepted = svc(db).accept(invitee.id, raw)
        assert accepted.role == role and accepted.accepted_at is not None
        with pytest.raises(OrganizationInvitationError): svc(db).accept(invitee.id, raw)


def test_reissue_invalidates_old_and_adapter_failure_is_post_commit():
    with SessionLocal() as db:
        owner = user(db, "reissue-owner"); invitee = user(db, "reissue-invitee"); org, _ = shared(db, owner)
        first_adapter = FakeEmailDeliveryAdapter(capture_deliveries=True); svc(db, first_adapter).issue(owner.id, org.id, invitee.email, "member")
        failed = svc(db, FakeEmailDeliveryAdapter(fail=True)).issue(owner.id, org.id, invitee.email, "admin")
        rows = OrganizationInvitationRepository(db).list_for_organization(org.id)
        assert len(rows) == 2 and rows[0].invalidated_at is not None and rows[1].invalidated_at is None and failed.delivery_failed
        with pytest.raises(OrganizationInvitationError): svc(db).accept(invitee.id, first_adapter.deliveries[0].raw_token)


def test_authorization_email_binding_existing_membership_and_invalid_states():
    with SessionLocal() as db:
        owner = user(db, "owner"); member = user(db, "member"); wrong = user(db, "wrong"); org, _ = shared(db, owner)
        db.add(OrganizationMembership(organization_id=org.id, user_id=member.id, role="member")); db.commit()
        with pytest.raises(OrganizationInvitationAlreadyMemberError): svc(db).issue(owner.id, org.id, member.email, "member")
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True); svc(db, adapter).issue(owner.id, org.id, wrong.email, "member")
        with pytest.raises(OrganizationInvitationError): svc(db).accept(member.id, adapter.deliveries[0].raw_token)
        with pytest.raises(OrganizationInvitationError): svc(db).issue(owner.id, org.id, wrong.email, "owner")
        with pytest.raises(OrganizationInvitationError): svc(db).issue(member.id, org.id, wrong.email, "member")


@pytest.mark.parametrize("actor_role, invitation_role", [("admin", "member"), ("admin", "admin")])
def test_admin_can_issue_the_roles_allowed_by_policy(actor_role, invitation_role):
    with SessionLocal() as db:
        owner = user(db, "owner"); admin = user(db, "admin"); invitee = user(db, "invitee")
        org, _ = shared(db, owner)
        db.add(OrganizationMembership(organization_id=org.id, user_id=admin.id, role=actor_role)); db.commit()
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        issued = svc(db, adapter).issue(admin.id, org.id, invitee.email, invitation_role)
        assert issued.role == invitation_role and adapter.call_count == 1


def test_issue_rejects_personal_blocked_and_invalid_email():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee")
        personal = Organization(
            kind="personal", name="Personal", slug=f"{PREFIX}{uuid4().hex}",
            personal_owner_user_id=owner.id,
        )
        db.add(personal); db.flush()
        db.add(OrganizationMembership(organization_id=personal.id, user_id=owner.id, role="owner"))
        blocked, _ = shared(db, owner, blocked=True); db.commit()
        with pytest.raises(OrganizationInvitationError): svc(db).issue(owner.id, personal.id, invitee.email, "member")
        with pytest.raises(OrganizationInvitationError): svc(db).issue(owner.id, blocked.id, invitee.email, "member")
        with pytest.raises(ValueError): svc(db).issue(owner.id, blocked.id, "not-an-email", "member")


def test_expired_invalidated_and_cross_organization_revoke_are_rejected():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); other = user(db, "other"); org, _ = shared(db, owner); second, _ = shared(db, other)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        issued = OrganizationInvitationApplicationService(
            db, PEPPER, adapter, ttl=timedelta(seconds=1)
        ).issue(owner.id, org.id, invitee.email, "member")
        sleep(1.1)
        with pytest.raises(OrganizationInvitationError): svc(db).accept(invitee.id, adapter.deliveries[0].raw_token)
        with pytest.raises(OrganizationInvitationError): svc(db).revoke(other.id, second.id, issued.invitation_id)


def test_concurrent_accept_creates_one_membership():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner); adapter = FakeEmailDeliveryAdapter(capture_deliveries=True); svc(db, adapter).issue(owner.id, org.id, invitee.email, "member"); token = adapter.deliveries[0].raw_token; invitee_id = invitee.id
    def accept():
        with SessionLocal() as db:
            try: svc(db).accept(invitee_id, token); return True
            except OrganizationInvitationError: return False
    with ThreadPoolExecutor(max_workers=2) as executor: outcomes = list(executor.map(lambda _: accept(), range(2)))
    assert outcomes.count(True) == 1 and outcomes.count(False) == 1


def test_revoke_and_unknown_or_tampered_tokens_do_not_create_membership():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        issued = svc(db, adapter).issue(owner.id, org.id, invitee.email, "member")
        svc(db).revoke(owner.id, org.id, issued.invitation_id)
        with pytest.raises(OrganizationInvitationError): svc(db).accept(invitee.id, adapter.deliveries[0].raw_token)
        with pytest.raises(OrganizationInvitationError): svc(db).accept(invitee.id, "not-a-real-token")
        with pytest.raises(OrganizationInvitationError): svc(db).accept(invitee.id, adapter.deliveries[0].raw_token + "x")
        assert not OrganizationMembershipRepository(db).get_active_for_user(org.id, invitee.id)


def test_issue_rollback_restores_previous_active_invitation(monkeypatch):
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        service = svc(db, adapter); service.issue(owner.id, org.id, invitee.email, "member")
        previous = adapter.deliveries[0].raw_token

        def fail_create(_row):
            raise RuntimeError("injected create failure")

        monkeypatch.setattr(service._invitations, "create", fail_create)
        with pytest.raises(RuntimeError): service.issue(owner.id, org.id, invitee.email, "admin")
        active = OrganizationInvitationRepository(db).get_active_by_email(org.id, invitee.email_normalized)
        assert active is not None
        accepted = svc(db).accept(invitee.id, previous)
        assert accepted.role == "member"


def test_accept_rollback_after_membership_flush_leaves_token_usable(monkeypatch):
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        service = svc(db, adapter); service.issue(owner.id, org.id, invitee.email, "member")
        token = adapter.deliveries[0].raw_token

        def fail_mark_accepted(_row):
            raise RuntimeError("injected acceptance failure")

        monkeypatch.setattr(service._invitations, "mark_accepted", fail_mark_accepted)
        with pytest.raises(RuntimeError): service.accept(invitee.id, token)
        assert not OrganizationMembershipRepository(db).get_active_for_user(org.id, invitee.id)
        assert svc(db).accept(invitee.id, token).accepted_at is not None


def test_concurrent_issue_keeps_exactly_one_active_invitation():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner)
        owner_id, invitee_email, org_id = owner.id, invitee.email, org.id

    def issue():
        with SessionLocal() as db:
            try:
                adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
                svc(db, adapter).issue(owner_id, org_id, invitee_email, "member")
                return True
            except OrganizationInvitationError:
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: issue(), range(2)))
    with SessionLocal() as db:
        rows = OrganizationInvitationRepository(db).list_for_organization(org_id)
        assert outcomes == [True, True]
        assert len(rows) == 2 and sum(row.invalidated_at is None for row in rows) == 1


def test_accept_and_revoke_are_serializable_for_the_same_invitation():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        issued = svc(db, adapter).issue(owner.id, org.id, invitee.email, "member")
        token, owner_id, invitee_id, org_id, invitation_id = adapter.deliveries[0].raw_token, owner.id, invitee.id, org.id, issued.invitation_id

    def accept():
        with SessionLocal() as db:
            try: svc(db).accept(invitee_id, token); return "accepted"
            except OrganizationInvitationError: return "rejected"

    def revoke():
        with SessionLocal() as db:
            try: svc(db).revoke(owner_id, org_id, invitation_id); return "revoked"
            except OrganizationInvitationError: return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda operation: operation(), (accept, revoke)))
    with SessionLocal() as db:
        row = db.get(OrganizationInvitation, invitation_id)
        membership = OrganizationMembershipRepository(db).get_active_for_user(org_id, invitee_id)
        assert row is not None and not (row.accepted_at and row.invalidated_at)
        assert (membership is not None) == (row.accepted_at is not None)
        assert sorted(outcomes) in (["accepted", "rejected"], ["rejected", "revoked"])


def test_reissue_and_accept_are_serializable():
    with SessionLocal() as db:
        owner = user(db, "owner"); invitee = user(db, "invitee"); org, _ = shared(db, owner)
        adapter = FakeEmailDeliveryAdapter(capture_deliveries=True)
        svc(db, adapter).issue(owner.id, org.id, invitee.email, "member")
        token, owner_id, invitee_id, email, org_id = adapter.deliveries[0].raw_token, owner.id, invitee.id, invitee.email, org.id

    def accept():
        with SessionLocal() as db:
            try: svc(db).accept(invitee_id, token); return "accepted"
            except OrganizationInvitationError: return "rejected"

    def reissue():
        with SessionLocal() as db:
            try: svc(db, FakeEmailDeliveryAdapter(capture_deliveries=True)).issue(owner_id, org_id, email, "member"); return "reissued"
            except OrganizationInvitationError: return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda operation: operation(), (accept, reissue)))
    with SessionLocal() as db:
        rows = OrganizationInvitationRepository(db).list_for_organization(org_id)
        membership = OrganizationMembershipRepository(db).get_active_for_user(org_id, invitee_id)
        active = [row for row in rows if row.invalidated_at is None and row.accepted_at is None]
        assert not (membership and active)
        assert sorted(outcomes) in (["accepted", "rejected"], ["reissued", "rejected"])

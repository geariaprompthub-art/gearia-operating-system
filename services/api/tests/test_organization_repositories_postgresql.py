"""Real PostgreSQL contracts for transaction-neutral P3A repositories."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal
from app.models.organization import (
    Organization,
    OrganizationInvitation,
    OrganizationKind,
    OrganizationMembership,
    OrganizationMembershipRole,
)
from app.models.user import User
from app.repositories.organization_invitation_repository import OrganizationInvitationRepository
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository


PREFIX = "p3a-organization-repository-"


@pytest.fixture(autouse=True)
def cleanup_organization_rows() -> None:
    """Remove only records owned by this integration suite."""

    def cleanup() -> None:
        with SessionLocal() as database:
            user_ids = list(
                database.scalars(
                    select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))
                )
            )
            if user_ids:
                organization_ids = list(
                    database.scalars(
                        select(Organization.id).where(Organization.personal_owner_user_id.in_(user_ids))
                    )
                )
                if organization_ids:
                    membership_ids = select(OrganizationMembership.id).where(
                        OrganizationMembership.organization_id.in_(organization_ids)
                    )
                    database.execute(
                        delete(OrganizationInvitation).where(
                            OrganizationInvitation.organization_id.in_(organization_ids)
                        )
                    )
                    database.execute(
                        delete(OrganizationMembership).where(OrganizationMembership.id.in_(membership_ids))
                    )
                    database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
                database.execute(delete(User).where(User.id.in_(user_ids)))
            database.commit()

    cleanup()
    try:
        yield
    finally:
        cleanup()


def make_user(suffix: str) -> User:
    email = f"{PREFIX}{suffix}@example.com"
    return User(email=email, email_normalized=email, password_hash="argon2id-test-hash")


def make_organization(owner_id: UUID, suffix: str, *, kind: str = OrganizationKind.PERSONAL.value) -> Organization:
    values: dict[str, object] = {
        "kind": kind,
        "name": f"Organization {suffix}",
        "slug": f"p3a-{suffix}-{uuid4().hex[:8]}",
    }
    if kind == OrganizationKind.PERSONAL.value:
        values["personal_owner_user_id"] = owner_id
    return Organization(**values)


def make_membership(organization_id: UUID, user_id: UUID, role: str = "owner") -> OrganizationMembership:
    return OrganizationMembership(organization_id=organization_id, user_id=user_id, role=role)


def make_invitation(organization_id: UUID, membership_id: UUID, suffix: str) -> OrganizationInvitation:
    return OrganizationInvitation(
        organization_id=organization_id,
        invited_email_normalized=f"invite-{suffix}@example.com",
        role="member",
        token_hash=uuid4().hex + uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        created_by_membership_id=membership_id,
    )


def seed_organization() -> tuple[UUID, UUID, UUID]:
    with SessionLocal() as database:
        user = make_user(uuid4().hex)
        database.add(user)
        database.flush()
        organization = OrganizationRepository(database).create(make_organization(user.id, uuid4().hex))
        membership = OrganizationMembershipRepository(database).create(
            make_membership(organization.id, user.id)
        )
        database.commit()
        return user.id, organization.id, membership.id


def test_organization_repository_flushes_scopes_access_and_external_rollback() -> None:
    with SessionLocal() as database:
        first, second = make_user("first"), make_user("second")
        database.add_all([first, second])
        database.flush()
        organizations = OrganizationRepository(database)
        memberships = OrganizationMembershipRepository(database)
        personal = organizations.create(make_organization(first.id, "personal"))
        shared = organizations.create(make_organization(first.id, "shared", kind="shared"))
        first_membership = memberships.create(make_membership(personal.id, first.id))
        memberships.create(make_membership(shared.id, first.id, "admin"))
        revoked = memberships.create(make_membership(shared.id, second.id))
        memberships.revoke(revoked)

        assert organizations.get_by_id(personal.id) is personal
        assert organizations.get_personal_by_owner_user_id(first.id) is personal
        assert [item.id for item in organizations.list_accessible_by_user(first.id)] == [personal.id, shared.id]
        assert organizations.list_accessible_by_user(second.id) == []
        organizations.update_name_and_slug(personal, "Renamed", "p3a-renamed")
        organizations.block(personal)
        assert personal.name == "Renamed" and personal.blocked_at is not None
        assert memberships.get_by_id_in_organization(personal.id, first_membership.id) is first_membership
        database.rollback()

    with SessionLocal() as verification:
        assert verification.get(Organization, personal.id) is None
        assert verification.get(OrganizationMembership, first_membership.id) is None


def test_membership_repository_is_organization_scoped_and_retains_history_explicitly() -> None:
    with SessionLocal() as database:
        user, other = make_user("member"), make_user("other")
        database.add_all([user, other]); database.flush()
        organizations = OrganizationRepository(database)
        memberships = OrganizationMembershipRepository(database)
        first = organizations.create(make_organization(user.id, "first"))
        second = organizations.create(make_organization(other.id, "second"))
        member = memberships.create(make_membership(first.id, user.id, "owner"))
        memberships.create(make_membership(second.id, user.id, "member"))

        assert memberships.get_by_id_in_organization(second.id, member.id) is None
        assert memberships.get_active_for_user(first.id, user.id) is member
        assert [item.id for item in memberships.list_active_for_organization(first.id)] == [member.id]
        assert memberships.count_active_owners(first.id) == 1
        memberships.update_role(member, "admin")
        assert member.role == "admin" and memberships.count_active_owners(first.id) == 0
        memberships.revoke(member)
        assert memberships.get_by_id_in_organization(first.id, member.id) is None
        assert memberships.get_historical_by_id_in_organization(first.id, member.id) is member
        database.rollback()


def test_membership_repository_database_invariants_and_external_recovery() -> None:
    user_id, organization_id, _ = seed_organization()
    with SessionLocal() as database:
        repository = OrganizationMembershipRepository(database)
        with pytest.raises(IntegrityError):
            repository.create(make_membership(organization_id, user_id, "member"))
        database.rollback()
        assert database.is_active
        existing = repository.get_active_for_user(organization_id, user_id)
        assert existing is not None
        repository.revoke(existing)
        historical = repository.create(make_membership(organization_id, user_id, "member"))
        assert historical.id is not None
        database.rollback()


def test_invitation_repository_scopes_hashes_and_allows_replacement_after_invalidation() -> None:
    user_id, organization_id, membership_id = seed_organization()
    with SessionLocal() as database:
        users = OrganizationRepository(database)
        memberships = OrganizationMembershipRepository(database)
        invitations = OrganizationInvitationRepository(database)
        original = invitations.create(make_invitation(organization_id, membership_id, "first"))
        assert invitations.get_by_token_hash(original.token_hash) is original
        assert invitations.get_by_id_in_organization(organization_id, original.id) is original
        assert invitations.get_active_by_email(organization_id, original.invited_email_normalized) is original
        assert invitations.invalidate_active_for_email(organization_id, original.invited_email_normalized) == 1
        replacement = invitations.create(make_invitation(organization_id, membership_id, "replacement"))
        replacement.invited_email_normalized = original.invited_email_normalized
        database.flush()
        invitations.mark_accepted(replacement)
        assert replacement.accepted_at is not None
        assert [item.id for item in invitations.list_for_organization(organization_id)] == [original.id, replacement.id]

        other = make_user("invite-other")
        database.add(other); database.flush()
        other_org = users.create(make_organization(other.id, "invite-other"))
        other_membership = memberships.create(make_membership(other_org.id, other.id))
        foreign = invitations.create(make_invitation(other_org.id, other_membership.id, "foreign"))
        assert invitations.get_by_id_in_organization(organization_id, foreign.id) is None
        assert invitations.get_by_id_in_organization(other_org.id, original.id) is None
        database.rollback()


def test_invitation_repository_propagates_unique_hash_and_flushes_without_commit() -> None:
    _, organization_id, membership_id = seed_organization()
    with SessionLocal() as database:
        repository = OrganizationInvitationRepository(database)
        first = repository.create(make_invitation(organization_id, membership_id, "hash"))
        database.commit()
        first_hash = first.token_hash
        duplicate = make_invitation(organization_id, membership_id, "different")
        duplicate.token_hash = first_hash
        with pytest.raises(IntegrityError):
            repository.create(duplicate)
        database.rollback()
        assert database.is_active
        persisted = repository.get_by_token_hash(first_hash)
        assert persisted is not None
        repository.invalidate(persisted)
        assert persisted.invalidated_at is not None
        database.rollback()


def test_repositories_never_own_commit_or_rollback() -> None:
    class SpySession:
        def __init__(self) -> None:
            self.flush_calls = self.commit_calls = self.rollback_calls = 0

        def add(self, _: object) -> None:
            pass

        def flush(self) -> None:
            self.flush_calls += 1

        def execute(self, *_: object) -> object:
            return type("Result", (), {"rowcount": 0})()

    spy = SpySession()
    user_id = uuid4()
    organization = make_organization(user_id, "spy")
    organization.id = uuid4()
    membership = make_membership(organization.id, user_id)
    membership.id = uuid4()
    OrganizationRepository(spy).create(organization)
    OrganizationRepository(spy).update_name_and_slug(organization, "Name", "p3a-spy-name")
    OrganizationMembershipRepository(spy).create(membership)
    OrganizationMembershipRepository(spy).update_role(membership, "admin")
    OrganizationInvitationRepository(spy).create(make_invitation(organization.id, membership.id, "spy"))
    assert spy.flush_calls == 5 and spy.commit_calls == 0 and spy.rollback_calls == 0


@pytest.mark.parametrize("lock_kind", ["organization", "owners", "invitation"])
def test_postgresql_for_update_locks_organization_primitives(lock_kind: str) -> None:
    _, organization_id, membership_id = seed_organization()
    invitation_hash: str | None = None
    if lock_kind == "invitation":
        with SessionLocal() as database:
            invitation = OrganizationInvitationRepository(database).create(
                make_invitation(organization_id, membership_id, "lock")
            )
            database.commit()
            invitation_hash = invitation.token_hash

    ready, release = Event(), Event()

    def lock(database: object) -> object:
        if lock_kind == "organization":
            return OrganizationRepository(database).get_by_id_for_update(organization_id)  # type: ignore[arg-type]
        if lock_kind == "owners":
            return OrganizationMembershipRepository(database).list_active_owners_for_update(organization_id)  # type: ignore[arg-type]
        assert invitation_hash is not None
        return OrganizationInvitationRepository(database).get_by_token_hash_for_update(invitation_hash)  # type: ignore[arg-type]

    def owner() -> None:
        with SessionLocal() as database:
            assert lock(database) is not None
            ready.set()
            assert release.wait(timeout=5)
            database.rollback()

    def contender() -> str:
        assert ready.wait(timeout=5)
        with SessionLocal() as database:
            database.execute(text("SET LOCAL lock_timeout = '250ms'"))
            try:
                lock(database)
            except DBAPIError:
                database.rollback()
                return "blocked"
            return "unexpected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner_future = executor.submit(owner)
        contender_future = executor.submit(contender)
        assert contender_future.result(timeout=5) == "blocked"
        release.set()
        owner_future.result(timeout=5)


@pytest.mark.parametrize("kind", ["membership", "invitation"])
def test_postgresql_concurrent_active_uniqueness(kind: str) -> None:
    user_id, organization_id, membership_id = seed_organization()
    with SessionLocal() as database:
        other = make_user(f"{kind}-concurrent")
        database.add(other)
        database.commit()
        other_user_id = other.id

    ready = Event()

    def attempt(first: bool) -> str:
        with SessionLocal() as database:
            try:
                if kind == "membership":
                    row = make_membership(organization_id, other_user_id, "member")
                    if first:
                        ready.set()
                    else:
                        assert ready.wait(timeout=5)
                    OrganizationMembershipRepository(database).create(row)
                else:
                    row = make_invitation(organization_id, membership_id, "concurrent")
                    row.invited_email_normalized = "shared-invite@example.com"
                    if first:
                        ready.set()
                    else:
                        assert ready.wait(timeout=5)
                    OrganizationInvitationRepository(database).create(row)
                database.commit()
                return "created"
            except IntegrityError:
                database.rollback()
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, [True, False]))
    assert sorted(outcomes) == ["created", "duplicate"]
    with SessionLocal() as verification:
        if kind == "membership":
            active = verification.scalar(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == user_id,
                    OrganizationMembership.revoked_at.is_(None),
                )
            )
        else:
            active = verification.scalar(
                select(OrganizationInvitation).where(
                    OrganizationInvitation.organization_id == organization_id,
                    OrganizationInvitation.invited_email_normalized == "shared-invite@example.com",
                    OrganizationInvitation.accepted_at.is_(None),
                    OrganizationInvitation.invalidated_at.is_(None),
                )
            )
        assert active is not None

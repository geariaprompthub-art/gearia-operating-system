"""PostgreSQL contracts for shared-membership lifecycle and last-owner protection."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User, UserStatus
from app.models.workspace import Workspace
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context_resolver import OrganizationContextResolver
from app.services.organization_membership_application_service import (
    LastOrganizationOwnerError,
    OrganizationMembershipApplicationService,
    OrganizationMembershipLifecycleError,
)


PREFIX = "p3a-membership-lifecycle-"


def service(database):
    organizations = OrganizationRepository(database)
    memberships = OrganizationMembershipRepository(database)
    return OrganizationMembershipApplicationService(
        database, organizations, memberships, OrganizationContextResolver(organizations, memberships)
    )


def cleanup_rows(database) -> None:
    organization_ids = list(database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%"))))
    user_ids = list(database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))))
    database.execute(delete(Workspace).where(Workspace.organization_id.in_(organization_ids)))
    database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organization_ids)))
    database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
    database.execute(delete(User).where(User.id.in_(user_ids)))
    database.commit()


@pytest.fixture(autouse=True)
def clean_database() -> None:
    with SessionLocal() as database:
        cleanup_rows(database)
    try:
        yield
    finally:
        with SessionLocal() as database:
            cleanup_rows(database)


def user(database, label: str) -> User:
    email = f"{PREFIX}{label}-{uuid4().hex}@example.com"
    item = User(email=email, email_normalized=email, password_hash="hash", status=UserStatus.ACTIVE.value)
    database.add(item)
    database.flush()
    return item


def graph(database, roles: list[str], *, kind: str = "shared", status: str = "active") -> tuple[Organization, list[OrganizationMembership]]:
    users = [user(database, f"user-{index}") for index in range(len(roles))]
    organization = Organization(
        kind=kind,
        name="Lifecycle",
        slug=f"{PREFIX}{uuid4().hex}",
        status=status,
        personal_owner_user_id=users[0].id if kind == "personal" else None,
    )
    database.add(organization)
    database.flush()
    memberships = [
        OrganizationMembership(organization_id=organization.id, user_id=item.id, role=role)
        for item, role in zip(users, roles, strict=True)
    ]
    database.add_all(memberships)
    database.flush()
    database.commit()
    return organization, memberships


@pytest.mark.parametrize(
    ("initial", "target", "expected"),
    [
        ("member", "admin", "admin"),
        ("admin", "member", "member"),
        ("member", "owner", "owner"),
        ("admin", "owner", "owner"),
    ],
)
def test_owner_can_change_non_owner_roles(initial: str, target: str, expected: str) -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", initial])
        result = service(database).change_role(
            memberships[0].user_id, organization.id, memberships[1].id, target
        )
        assert result.role == expected and result.revoked_at is None
    with SessionLocal() as database:
        assert database.get(OrganizationMembership, result.membership_id).role == expected


@pytest.mark.parametrize("target_role", ["admin", "member"])
def test_owner_can_demote_an_owner_when_another_owner_remains(target_role: str) -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "owner"])
        result = service(database).change_role(
            memberships[0].user_id, organization.id, memberships[1].id, target_role
        )
        assert result.role == target_role


@pytest.mark.parametrize("target_role", ["admin", "member"])
def test_last_owner_cannot_be_demoted(target_role: str) -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner"])
        with pytest.raises(LastOrganizationOwnerError, match="organization membership unavailable"):
            service(database).change_role(memberships[0].user_id, organization.id, memberships[0].id, target_role)
        assert database.get(OrganizationMembership, memberships[0].id).role == "owner"


@pytest.mark.parametrize("role", ["member", "admin", "owner"])
def test_owner_can_revoke_allowed_target_with_last_owner_protection(role: str) -> None:
    roles = ["owner", role] if role != "owner" else ["owner", "owner"]
    with SessionLocal() as database:
        organization, memberships = graph(database, roles)
        result = service(database).revoke(memberships[0].user_id, organization.id, memberships[1].id)
        assert result.revoked_at is not None
        assert database.get(OrganizationMembership, memberships[1].id).revoked_at is not None


def test_last_owner_cannot_be_revoked_and_revocation_is_not_idempotent() -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "member"])
        with pytest.raises(LastOrganizationOwnerError):
            service(database).revoke(memberships[0].user_id, organization.id, memberships[0].id)
        service(database).revoke(memberships[0].user_id, organization.id, memberships[1].id)
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).revoke(memberships[0].user_id, organization.id, memberships[1].id)
        created_again = OrganizationMembership(
            organization_id=organization.id, user_id=memberships[1].user_id, role="member"
        )
        database.add(created_again)
        database.flush()
        assert created_again.revoked_at is None
        database.rollback()


def test_admin_can_revoke_member_but_cannot_change_roles_or_remove_admin_or_owner() -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "admin", "member"])
        result = service(database).revoke(memberships[1].user_id, organization.id, memberships[2].id)
        assert result.revoked_at is not None
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).change_role(memberships[1].user_id, organization.id, memberships[0].id, "member")
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).revoke(memberships[1].user_id, organization.id, memberships[1].id)
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).revoke(memberships[1].user_id, organization.id, memberships[0].id)


def test_member_cannot_mutate_memberships() -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "member", "member"])
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).change_role(memberships[1].user_id, organization.id, memberships[2].id, "admin")
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).revoke(memberships[1].user_id, organization.id, memberships[2].id)


@pytest.mark.parametrize("kind,status", [("personal", "active"), ("shared", "blocked")])
def test_personal_and_blocked_organizations_reject_membership_mutations(kind: str, status: str) -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "member"], kind=kind, status=status)
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).change_role(memberships[0].user_id, organization.id, memberships[1].id, "admin")
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).revoke(memberships[0].user_id, organization.id, memberships[1].id)


def test_owner_self_demotion_and_self_revocation_are_allowed_when_another_owner_remains() -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "owner"])
        demoted = service(database).change_role(memberships[0].user_id, organization.id, memberships[0].id, "admin")
        assert demoted.role == "admin"
        second_organization, second_memberships = graph(database, ["owner", "owner"])
        revoked = service(database).revoke(
            second_memberships[1].user_id, second_organization.id, second_memberships[1].id
        )
        assert revoked.revoked_at is not None


def test_list_and_cross_organization_target_are_scoped_and_orm_free() -> None:
    with SessionLocal() as database:
        first, first_memberships = graph(database, ["owner", "member"])
        second, second_memberships = graph(database, ["owner", "member"])
        listed = service(database).list_active(first_memberships[0].user_id, first.id)
        assert [item.membership_id for item in listed] == [item.id for item in first_memberships]
        assert not hasattr(listed[0], "_sa_instance_state")
        with pytest.raises(OrganizationMembershipLifecycleError):
            service(database).revoke(first_memberships[0].user_id, first.id, second_memberships[1].id)
        assert database.get(OrganizationMembership, second_memberships[1].id).revoked_at is None


@pytest.mark.parametrize(("first_action", "second_action"), [("revoke", "revoke"), ("demote", "demote"), ("revoke", "demote")])
def test_concurrent_owner_reductions_never_leave_zero_owners(first_action: str, second_action: str) -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "owner"])
        organization_id = organization.id
        pairs = [(item.user_id, item.id) for item in memberships]

    def mutate(actor_id: UUID, membership_id: UUID, action: str) -> str:
        with SessionLocal() as database:
            try:
                if action == "revoke":
                    service(database).revoke(actor_id, organization_id, membership_id)
                else:
                    service(database).change_role(actor_id, organization_id, membership_id, "admin")
                return "applied"
            except LastOrganizationOwnerError:
                return "last_owner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda value: mutate(*value), [(pairs[0][0], pairs[0][1], first_action), (pairs[1][0], pairs[1][1], second_action)]))
    assert sorted(outcomes) == ["applied", "last_owner"]
    with SessionLocal() as database:
        count = OrganizationMembershipRepository(database).count_active_owners(organization_id)
        assert count == 1


def test_concurrent_promotions_of_distinct_targets_preserve_membership_integrity() -> None:
    with SessionLocal() as database:
        organization, memberships = graph(database, ["owner", "member", "admin"])
        organization_id, actor_id = organization.id, memberships[0].user_id
        targets = [item.id for item in memberships[1:]]

    def promote(target_id: UUID) -> str:
        with SessionLocal() as database:
            return service(database).change_role(actor_id, organization_id, target_id, "owner").role

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(promote, targets)) == ["owner", "owner"]
    with SessionLocal() as database:
        owners = OrganizationMembershipRepository(database).count_active_owners(organization_id)
        memberships_after = OrganizationMembershipRepository(database).list_active_for_organization(organization_id)
        assert owners == 3 and len(memberships_after) == 3

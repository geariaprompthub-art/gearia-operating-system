"""Unit and PostgreSQL contracts for the P3A organization authorization boundary."""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMembership,
    OrganizationMembershipRole,
    OrganizationStatus,
)
from app.models.user import User
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.organization_context import OrganizationContext
from app.services.organization_context_resolver import (
    OrganizationContextResolver,
    OrganizationContextUnavailableError,
)
from app.services.organization_policy import OrganizationAction, OrganizationPolicy


PREFIX = "p3a-organization-context-"


def context(role: OrganizationMembershipRole = OrganizationMembershipRole.OWNER) -> OrganizationContext:
    return OrganizationContext(
        user_id=uuid4(),
        organization_id=uuid4(),
        organization_kind=OrganizationKind.SHARED,
        organization_status=OrganizationStatus.ACTIVE,
        membership_id=uuid4(),
        membership_role=role,
    )


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (OrganizationMembershipRole.OWNER, frozenset(OrganizationAction)),
        (
            OrganizationMembershipRole.ADMIN,
            frozenset(
                {
                    OrganizationAction.READ_ORGANIZATION,
                    OrganizationAction.UPDATE_ORGANIZATION,
                    OrganizationAction.LIST_MEMBERSHIPS,
                    OrganizationAction.INVITE_MEMBER,
                    OrganizationAction.INVALIDATE_INVITATION,
                    OrganizationAction.REMOVE_MEMBER,
                    OrganizationAction.CREATE_WORKSPACE,
                    OrganizationAction.UPDATE_WORKSPACE,
                    OrganizationAction.USE_WORKSPACE,
                }
            ),
        ),
        (
            OrganizationMembershipRole.MEMBER,
            frozenset(
                {
                    OrganizationAction.READ_ORGANIZATION,
                    OrganizationAction.LIST_MEMBERSHIPS,
                    OrganizationAction.USE_WORKSPACE,
                }
            ),
        ),
    ],
)
def test_organization_policy_is_complete_and_default_denies(
    role: OrganizationMembershipRole, allowed: frozenset[OrganizationAction]
) -> None:
    value = context(role)
    assert {action for action in OrganizationAction if OrganizationPolicy.is_allowed(value, action)} == allowed


def test_context_is_immutable_orm_free_and_active_only() -> None:
    value = context()
    assert [field.name for field in fields(value)] == [
        "user_id", "organization_id", "organization_kind", "organization_status", "membership_id", "membership_role"
    ]
    assert not hasattr(value, "_sa_instance_state")
    with pytest.raises(FrozenInstanceError):
        value.user_id = uuid4()  # type: ignore[misc]
    with pytest.raises(ValueError):
        OrganizationContext(
            user_id=uuid4(), organization_id=uuid4(), organization_kind=OrganizationKind.SHARED,
            organization_status=OrganizationStatus.BLOCKED, membership_id=uuid4(),
            membership_role=OrganizationMembershipRole.MEMBER,
        )


def test_policy_rejects_unknown_action_and_role() -> None:
    value = context()
    with pytest.raises(ValueError):
        OrganizationPolicy.is_allowed(value, "read_organization")  # type: ignore[arg-type]
    object.__setattr__(value, "membership_role", "unknown")
    with pytest.raises(ValueError):
        OrganizationPolicy.is_allowed(value, OrganizationAction.READ_ORGANIZATION)


class FakeOrganizations:
    def __init__(self, organization: Organization | None, personal: Organization | None = None) -> None:
        self.organization = organization
        self.personal = personal

    def get_by_id(self, _: UUID) -> Organization | None:
        return self.organization

    def get_personal_by_owner_user_id(self, _: UUID) -> Organization | None:
        return self.personal


class FakeMemberships:
    def __init__(self, membership: OrganizationMembership | None) -> None:
        self.membership = membership

    def get_active_for_user(self, _: UUID, __: UUID) -> OrganizationMembership | None:
        return self.membership


def organization(*, owner_id: UUID | None = None, status: str = "active") -> Organization:
    return Organization(
        id=uuid4(), kind="personal" if owner_id else "shared", name="Organization", slug=f"org-{uuid4().hex}",
        status=status, personal_owner_user_id=owner_id,
    )


def membership(organization_id: UUID, user_id: UUID, role: str = "owner") -> OrganizationMembership:
    return OrganizationMembership(id=uuid4(), organization_id=organization_id, user_id=user_id, role=role)


@pytest.mark.parametrize("org_status, has_membership", [("blocked", True), ("active", False)])
def test_resolver_rejects_unavailable_organization_uniformly(org_status: str, has_membership: bool) -> None:
    user_id = uuid4()
    org = organization(status=org_status)
    member = membership(org.id, user_id) if has_membership else None
    resolver = OrganizationContextResolver(FakeOrganizations(org), FakeMemberships(member))  # type: ignore[arg-type]
    with pytest.raises(OrganizationContextUnavailableError, match="organization access unavailable"):
        resolver.resolve(user_id, org.id)


def test_resolver_rejects_missing_and_cross_organization_membership() -> None:
    user_id = uuid4()
    with pytest.raises(OrganizationContextUnavailableError):
        OrganizationContextResolver(FakeOrganizations(None), FakeMemberships(None)).resolve(user_id, uuid4())  # type: ignore[arg-type]
    org = organization()
    foreign = membership(uuid4(), user_id)
    resolver = OrganizationContextResolver(FakeOrganizations(org), FakeMemberships(foreign))  # type: ignore[arg-type]
    with pytest.raises(OrganizationContextUnavailableError):
        resolver.resolve(user_id, org.id)


def test_resolve_personal_requires_owner_consistency() -> None:
    user_id = uuid4()
    org = organization(owner_id=user_id)
    valid = membership(org.id, user_id)
    resolver = OrganizationContextResolver(FakeOrganizations(None, org), FakeMemberships(valid))  # type: ignore[arg-type]
    assert resolver.resolve_personal_for_user(user_id).membership_role is OrganizationMembershipRole.OWNER
    for invalid_organization, invalid_membership in [
        (org, None),
        (org, membership(org.id, user_id, "admin")),
        (organization(owner_id=uuid4()), valid),
    ]:
        resolver = OrganizationContextResolver(FakeOrganizations(None, invalid_organization), FakeMemberships(invalid_membership))  # type: ignore[arg-type]
        with pytest.raises(OrganizationContextUnavailableError):
            resolver.resolve_personal_for_user(user_id)


@pytest.fixture(autouse=True)
def cleanup_context_rows() -> None:
    def cleanup() -> None:
        with SessionLocal() as database:
            user_ids = list(database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))))
            if user_ids:
                organization_ids = select(Organization.id).where(Organization.personal_owner_user_id.in_(user_ids))
                database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organization_ids)))
                database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
                database.execute(delete(User).where(User.id.in_(user_ids)))
            database.commit()
    cleanup()
    try:
        yield
    finally:
        cleanup()


def seed_context(*, blocked: bool = False, role: str = "owner", revoked: bool = False) -> tuple[UUID, UUID]:
    with SessionLocal() as database:
        email = f"{PREFIX}{uuid4().hex}@example.com"
        user = User(email=email, email_normalized=email, password_hash="argon2id-test-hash")
        database.add(user); database.flush()
        org = OrganizationRepository(database).create(
            Organization(
                kind="personal", name="Personal", slug=f"context-{uuid4().hex}",
                status="blocked" if blocked else "active", personal_owner_user_id=user.id,
            )
        )
        member = OrganizationMembershipRepository(database).create(membership(org.id, user.id, role))
        if revoked:
            member.revoked_at = datetime.now(UTC)
            database.flush()
        database.commit()
        return user.id, org.id


def test_postgresql_resolver_is_read_only_scoped_and_rejects_blocked_or_revoked() -> None:
    user_id, organization_id = seed_context()
    with SessionLocal() as database:
        resolver = OrganizationContextResolver(
            OrganizationRepository(database), OrganizationMembershipRepository(database)
        )
        resolved = resolver.resolve(user_id, organization_id)
        assert resolved.user_id == user_id and resolved.organization_id == organization_id
        assert resolver.resolve_personal_for_user(user_id).membership_id == resolved.membership_id
        assert not database.new and not database.dirty and not database.deleted

    blocked_user, blocked_org = seed_context(blocked=True)
    revoked_user, revoked_org = seed_context(revoked=True)
    with SessionLocal() as database:
        resolver = OrganizationContextResolver(
            OrganizationRepository(database), OrganizationMembershipRepository(database)
        )
        with pytest.raises(OrganizationContextUnavailableError):
            resolver.resolve(blocked_user, blocked_org)
        with pytest.raises(OrganizationContextUnavailableError):
            resolver.resolve(revoked_user, revoked_org)
        with pytest.raises(OrganizationContextUnavailableError):
            resolver.resolve(uuid4(), organization_id)

"""PostgreSQL contracts for internal creation of shared organization aggregates."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User, UserStatus
from app.models.workspace import Workspace
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.personal_organization_provisioning_service import PersonalOrganizationProvisioningService
from app.services.shared_organization_application_service import (
    SharedOrganizationApplicationService,
    SharedOrganizationError,
    SharedOrganizationSlugConflictError,
)


PREFIX = "p3a-shared-organization-"


def service(database):
    return SharedOrganizationApplicationService(
        database,
        UserRepository(database),
        OrganizationRepository(database),
        OrganizationMembershipRepository(database),
        WorkspaceRepository(database),
    )


def cleanup(database, user_id: UUID) -> None:
    organization_ids = list(
        database.scalars(
            select(Organization.id)
            .outerjoin(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(
                (Organization.personal_owner_user_id == user_id)
                | (OrganizationMembership.user_id == user_id)
            )
        )
    )
    database.execute(delete(Workspace).where(Workspace.organization_id.in_(organization_ids)))
    database.execute(
        delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organization_ids))
    )
    database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
    database.execute(delete(User).where(User.id == user_id))


def cleanup_organizations(database, organization_ids: list[UUID]) -> None:
    database.execute(delete(Workspace).where(Workspace.organization_id.in_(organization_ids)))
    database.execute(
        delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organization_ids))
    )
    database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))


@pytest.fixture(autouse=True)
def cleanup_rows() -> None:
    def remove() -> None:
        with SessionLocal() as database:
            organization_ids = list(
                database.scalars(select(Organization.id).where(Organization.slug.like(f"{PREFIX}%")))
            )
            cleanup_organizations(database, organization_ids)
            user_ids = list(
                database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%")))
            )
            for user_id in user_ids:
                cleanup(database, user_id)
            database.commit()

    remove()
    try:
        yield
    finally:
        remove()


def create_user(database, suffix: str, status: str = UserStatus.ACTIVE.value) -> User:
    email = f"{PREFIX}{suffix}@example.com"
    user = User(email=email, email_normalized=email, password_hash="argon2id-test-hash", status=status)
    database.add(user)
    database.flush()
    return user


def test_create_commits_shared_organization_owner_membership_and_initial_workspace() -> None:
    with SessionLocal() as database:
        actor = create_user(database, "happy")
        actor_id = actor.id
        suffix = uuid4().hex
        result = service(database).create(
            actor.id, "  Product Team  ", f"{PREFIX}Product--Team-{suffix}"
        )

    with SessionLocal() as verification:
        organization = verification.get(Organization, result.organization_id)
        membership = verification.get(OrganizationMembership, result.owner_membership_id)
        workspace = verification.get(Workspace, result.workspace_id)
        assert organization is not None
        assert (organization.kind, organization.status, organization.personal_owner_user_id) == (
            "shared",
            "active",
            None,
        )
        assert (organization.name, organization.slug) == (
            "Product Team",
            f"p3a-shared-organization-product-team-{suffix}",
        )
        assert membership is not None and (membership.organization_id, membership.user_id, membership.role) == (
            organization.id,
                actor_id,
            "owner",
        )
        assert workspace is not None and (workspace.organization_id, workspace.owner_user_id, workspace.name) == (
            organization.id,
            None,
            "General",
        )
        assert [field.name for field in fields(result)] == [
            "organization_id", "owner_membership_id", "workspace_id", "name", "slug",
            "created_at", "updated_at",
        ]
        assert not hasattr(result, "_sa_instance_state")
        with pytest.raises(Exception):
            result.slug = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "status",
    [
        UserStatus.PENDING_VERIFICATION.value,
        UserStatus.LOCKED.value,
        UserStatus.SUSPENDED.value,
        UserStatus.ANONYMIZED.value,
    ],
)
def test_ineligible_actor_is_rejected_without_persistence(status: str) -> None:
    with SessionLocal() as database:
        actor = create_user(database, status, status)
        with pytest.raises(SharedOrganizationError, match="shared organization unavailable"):
            service(database).create(actor.id, "Team", f"team-{status}")
        assert database.scalar(select(Organization).where(Organization.slug == f"team-{status}")) is None
        database.rollback()


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("   ", "valid-slug"),
        ("Team", ""),
        ("Team", "bad slug"),
        ("Team", "-bad"),
        ("Team", "bad-"),
        ("Team", "owner@example.com"),
        ("x" * 121, "valid-slug"),
        ("Team", "x" * 121),
    ],
)
def test_invalid_name_or_slug_is_rejected_before_writes(name: str, slug: str) -> None:
    with SessionLocal() as database:
        actor = create_user(database, f"invalid-{uuid4().hex}")
        with pytest.raises(SharedOrganizationError, match="shared organization unavailable"):
            service(database).create(actor.id, name, slug)
        assert database.scalar(select(Organization).where(Organization.personal_owner_user_id == actor.id)) is None
        database.rollback()


def test_slug_conflict_rolls_back_and_maps_to_sanitized_error() -> None:
    with SessionLocal() as database:
        first = create_user(database, "slug-first")
        slug = f"{PREFIX}reserved-slug-{uuid4().hex}"
        result = service(database).create(first.id, "First", slug)
    with SessionLocal() as database:
        second = create_user(database, "slug-second")
        second_id = second.id
        with pytest.raises(SharedOrganizationSlugConflictError, match="shared organization unavailable"):
            service(database).create(second.id, "Second", slug)
        assert database.get(User, second_id) is None
        database.rollback()
    with SessionLocal() as verification:
        assert verification.scalar(select(Organization.id).where(Organization.slug == result.slug)) == result.organization_id
        assert verification.scalar(
            select(OrganizationMembership.id).where(OrganizationMembership.user_id == second_id)
        ) is None


def test_read_methods_require_active_membership_and_active_shared_organization() -> None:
    with SessionLocal() as database:
        owner = create_user(database, "reads-owner")
        outsider = create_user(database, "reads-outsider")
        owner_id, outsider_id = owner.id, outsider.id
        created = service(database).create(
            owner_id, "Readable", f"{PREFIX}readable-team-{uuid4().hex}"
        )
        reader = service(database)
        assert [item.organization_id for item in reader.list_accessible(owner_id)] == [created.organization_id]
        assert reader.get_shared_for_user(owner_id, created.organization_id).slug == created.slug
        assert [item.workspace_id for item in reader.list_workspaces(owner_id, created.organization_id)] == [
            created.workspace_id
        ]
        with pytest.raises(SharedOrganizationError):
            reader.get_shared_for_user(outsider_id, created.organization_id)
        membership = database.get(OrganizationMembership, created.owner_membership_id)
        assert membership is not None
        OrganizationMembershipRepository(database).revoke(membership)
        with pytest.raises(SharedOrganizationError):
            reader.get_shared_for_user(owner_id, created.organization_id)
        database.rollback()


def test_blocked_shared_organization_fails_closed_for_reads() -> None:
    with SessionLocal() as database:
        owner = create_user(database, "blocked")
        owner_id = owner.id
        created = service(database).create(
            owner_id, "Blocked", f"{PREFIX}blocked-team-{uuid4().hex}"
        )
        organization = database.get(Organization, created.organization_id)
        assert organization is not None
        OrganizationRepository(database).block(organization)
        with pytest.raises(SharedOrganizationError):
            service(database).list_workspaces(owner_id, created.organization_id)
        database.rollback()


@pytest.mark.parametrize("failure_stage", ["organization", "membership", "workspace", "commit"])
def test_injected_failure_rolls_back_every_staged_shared_aggregate(failure_stage: str, monkeypatch) -> None:
    with SessionLocal() as database:
        actor = create_user(database, f"rollback-{failure_stage}")
        creator = service(database)
        if failure_stage == "organization":
            original = creator._organizations.create
            monkeypatch.setattr(creator._organizations, "create", lambda organization: (_ for _ in ()).throw(RuntimeError("injected")))
        elif failure_stage == "membership":
            original = creator._memberships.create
            def fail_membership(membership):
                original(membership)
                raise RuntimeError("injected")
            monkeypatch.setattr(creator._memberships, "create", fail_membership)
        elif failure_stage == "workspace":
            original = creator._workspaces.create
            def fail_workspace(workspace):
                original(workspace)
                raise RuntimeError("injected")
            monkeypatch.setattr(creator._workspaces, "create", fail_workspace)
        else:
            monkeypatch.setattr(database, "commit", lambda: (_ for _ in ()).throw(RuntimeError("injected")))
        with pytest.raises(RuntimeError, match="injected"):
            creator.create(actor.id, "Rollback", f"rollback-{failure_stage}")
        assert database.scalar(select(Organization).where(Organization.slug == f"rollback-{failure_stage}")) is None
        assert database.get(User, actor.id) is None
        assert database.scalar(select(Workspace.id).where(Workspace.name == "General")) is None


def test_one_user_can_have_personal_and_multiple_shared_organizations() -> None:
    with SessionLocal() as database:
        actor = create_user(database, "multiple")
        actor_id = actor.id
        personal = PersonalOrganizationProvisioningService(
            OrganizationRepository(database), OrganizationMembershipRepository(database), WorkspaceRepository(database)
        ).provision_for_user(actor)
        database.commit()
        first = service(database).create(actor_id, "Shared A", f"{PREFIX}shared-a-{uuid4().hex}")
        second = service(database).create(actor_id, "Shared B", f"{PREFIX}shared-b-{uuid4().hex}")
    with SessionLocal() as verification:
        assert verification.get(Organization, personal.organization_id) is not None
        shared = list(verification.scalars(select(Organization).where(Organization.id.in_([first.organization_id, second.organization_id]))))
        assert len(shared) == 2
        assert all(item.kind == "shared" and item.personal_owner_user_id is None for item in shared)
        assert verification.scalar(select(Workspace.owner_user_id).where(Workspace.id == first.workspace_id)) is None
        assert verification.scalar(select(Workspace.owner_user_id).where(Workspace.id == second.workspace_id)) is None


def test_concurrent_same_slug_persists_one_complete_aggregate_only() -> None:
    with SessionLocal() as database:
        first = create_user(database, "concurrent-first")
        second = create_user(database, "concurrent-second")
        first_id, second_id = first.id, second.id
        database.commit()

    slug = f"{PREFIX}same-shared-slug-{uuid4().hex}"

    def create_for(actor_id: UUID):
        with SessionLocal() as database:
            try:
                return service(database).create(actor_id, "Concurrent", slug)
            except SharedOrganizationSlugConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_for, [first_id, second_id]))
    assert sum(result is not None for result in results) == 1
    with SessionLocal() as verification:
        organization = verification.scalar(select(Organization).where(Organization.slug == slug))
        assert organization is not None
        memberships = list(verification.scalars(select(OrganizationMembership).where(OrganizationMembership.organization_id == organization.id)))
        workspaces = list(verification.scalars(select(Workspace).where(Workspace.organization_id == organization.id)))
        assert len(memberships) == 1 and len(workspaces) == 1
        assert workspaces[0].owner_user_id is None

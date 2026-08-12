"""PostgreSQL contracts for idempotent personal organization provisioning."""

from dataclasses import FrozenInstanceError, fields
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.models.workspace import Workspace
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.personal_organization_provisioning_service import (
    PersonalOrganizationProvisioningError,
    PersonalOrganizationProvisioningService,
)


PREFIX = "p3a-personal-provisioning-"


def service(database):
    return PersonalOrganizationProvisioningService(
        OrganizationRepository(database),
        OrganizationMembershipRepository(database),
        WorkspaceRepository(database),
    )


def cleanup(database, user_id: UUID) -> None:
    organization_ids = select(Organization.id).where(Organization.personal_owner_user_id == user_id)
    database.execute(delete(Workspace).where(Workspace.owner_user_id == user_id))
    database.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(organization_ids)))
    database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
    database.execute(delete(User).where(User.id == user_id))


@pytest.fixture(autouse=True)
def cleanup_rows() -> None:
    def remove() -> None:
        with SessionLocal() as database:
            user_ids = list(database.scalars(select(User.id).where(User.email_normalized.like(f"{PREFIX}%"))))
            for user_id in user_ids:
                cleanup(database, user_id)
            database.commit()
    remove()
    try:
        yield
    finally:
        remove()


def create_user(database, suffix: str) -> User:
    email = f"{PREFIX}{suffix}@example.com"
    user = User(email=email, email_normalized=email, password_hash="argon2id-test-hash")
    database.add(user)
    database.flush()
    return user


def test_new_user_gets_atomic_personal_organization_owner_membership_and_workspace() -> None:
    with SessionLocal() as database:
        user = create_user(database, "new")
        result = service(database).provision_for_user(user)
        organization = database.get(Organization, result.organization_id)
        membership = database.get(OrganizationMembership, result.membership_id)
        workspace = database.get(Workspace, result.workspace_id)
        assert result.created_organization and result.created_membership and result.created_workspace
        assert organization is not None and organization.personal_owner_user_id == user.id
        assert organization.slug == f"personal-{user.id.hex}" and organization.name == "Personal workspace"
        assert membership is not None and membership.user_id == user.id and membership.role == "owner"
        assert workspace is not None and workspace.owner_user_id == user.id and workspace.organization_id == organization.id
        assert [field.name for field in fields(result)] == [
            "user_id", "organization_id", "membership_id", "workspace_id",
            "created_organization", "created_membership", "created_workspace",
        ]
        assert not hasattr(result, "_sa_instance_state")
        with pytest.raises(FrozenInstanceError):
            result.workspace_id = uuid4()  # type: ignore[misc]
        database.rollback()

    with SessionLocal() as verification:
        assert verification.scalar(select(User.id).where(User.email_normalized.like(f"{PREFIX}new%"))) is None


def test_existing_personal_graph_is_reused_without_duplicates() -> None:
    with SessionLocal() as database:
        user = create_user(database, "repeat")
        first = service(database).provision_for_user(user)
        database.commit()
        second = service(database).provision_for_user(user)
        assert (second.user_id, second.organization_id, second.membership_id, second.workspace_id) == (
            first.user_id, first.organization_id, first.membership_id, first.workspace_id
        )
        assert not second.created_organization and not second.created_membership and not second.created_workspace
        assert database.scalar(select(Organization).where(Organization.personal_owner_user_id == user.id)) is not None
        assert len(list(database.scalars(select(OrganizationMembership).where(OrganizationMembership.organization_id == first.organization_id)))) == 1
        database.rollback()


def test_legacy_workspace_is_associated_and_missing_membership_is_created() -> None:
    with SessionLocal() as database:
        user = create_user(database, "legacy")
        organization = OrganizationRepository(database).create(
            Organization(kind="personal", name="Personal workspace", slug=f"personal-{user.id.hex}", personal_owner_user_id=user.id)
        )
        workspace = WorkspaceRepository(database).create(Workspace(owner_user_id=user.id, organization_id=organization.id, name="Personal workspace"))
        database.commit()
        result = service(database).provision_for_user(user)
        assert result.organization_id == organization.id and result.workspace_id == workspace.id
        assert not result.created_organization and result.created_membership and not result.created_workspace
        assert workspace.organization_id == organization.id
        database.rollback()


def test_inconsistent_personal_graph_fails_without_repair() -> None:
    with SessionLocal() as database:
        user = create_user(database, "inconsistent")
        organization = OrganizationRepository(database).create(
            Organization(kind="personal", name="Personal workspace", slug=f"personal-{user.id.hex}", status="blocked", personal_owner_user_id=user.id)
        )
        database.commit()
        with pytest.raises(PersonalOrganizationProvisioningError, match="personal organization unavailable"):
            service(database).provision_for_user(user)
        assert database.get(Organization, organization.id).status == "blocked"
        database.rollback()

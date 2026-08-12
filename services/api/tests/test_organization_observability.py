"""Focused P3A structured-event contracts; records only permitted identifiers."""

import logging
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.core.structured_logging import SafeStructuredLogger
from app.models.organization import Organization, OrganizationInvitation, OrganizationMembership
from app.models.user import User
from app.models.workspace import Workspace
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.email_delivery import FakeEmailDeliveryAdapter
from app.services.organization_context_resolver import OrganizationContextResolver
from app.services.organization_invitation_application_service import OrganizationInvitationApplicationService
from app.services.organization_membership_application_service import LastOrganizationOwnerError, OrganizationMembershipApplicationService
from app.services.organization_metadata_application_service import OrganizationMetadataApplicationService, OrganizationMetadataError
from app.services.organization_workspace_application_service import OrganizationWorkspaceApplicationService
from app.services.shared_organization_application_service import SharedOrganizationApplicationService


P = "p3a-observability-"
PEPPER = "test-only-organization-observability-pepper"


class CapturingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, message: str, **fields: object) -> None:
        self.records.append((event, message, fields))

    def warning(self, event: str, message: str, **fields: object) -> None:
        self.records.append((event, message, fields))


@pytest.fixture(autouse=True)
def cleanup() -> None:
    def clear() -> None:
        with SessionLocal() as db:
            ids = list(db.scalars(select(Organization.id).where(Organization.slug.like(f"{P}%"))))
            db.execute(delete(OrganizationInvitation).where(OrganizationInvitation.organization_id.in_(ids)))
            db.execute(delete(Workspace).where(Workspace.organization_id.in_(ids)))
            db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_(ids)))
            db.execute(delete(Organization).where(Organization.id.in_(ids)))
            db.execute(delete(User).where(User.email_normalized.like(f"{P}%")))
            db.commit()
    clear()
    yield
    clear()


def user(db, label: str) -> User:
    email = f"{P}{label}-{uuid4().hex}@example.test"
    row = User(email=email, email_normalized=email, password_hash="test-hash", status="active")
    db.add(row); db.flush()
    return row


def services(db, logger: CapturingLogger):
    organizations = OrganizationRepository(db)
    memberships = OrganizationMembershipRepository(db)
    contexts = OrganizationContextResolver(organizations, memberships)
    return (
        SharedOrganizationApplicationService(db, UserRepository(db), organizations, memberships, WorkspaceRepository(db), contexts, logger),
        OrganizationMetadataApplicationService(db, organizations, contexts, structured_logger=logger),
        OrganizationMembershipApplicationService(db, organizations, memberships, contexts, structured_logger=logger),
        OrganizationWorkspaceApplicationService(db, WorkspaceRepository(db), contexts, logger),
    )


def events(logger: CapturingLogger) -> list[str]:
    return [event for event, _, _ in logger.records]


def test_organization_workspace_and_membership_events_include_only_safe_identifiers() -> None:
    with SessionLocal() as db:
        logger = CapturingLogger()
        creator, metadata, memberships, workspaces = services(db, logger)
        owner = user(db, "owner")
        created = creator.create(owner.id, "Observability", f"{P}{uuid4().hex}")
        metadata.update_name(owner.id, created.organization_id, "Renamed")
        workspaces.create_shared(owner.id, created.organization_id, "Operations")
        member = user(db, "member")
        membership = OrganizationMembership(organization_id=created.organization_id, user_id=member.id, role="member")
        db.add(membership); db.commit()
        memberships.change_role(owner.id, created.organization_id, membership.id, "admin")
        memberships.revoke(owner.id, created.organization_id, membership.id)
        assert {"organization.created", "organization.updated", "organization_workspace.created", "organization_membership.role_changed", "organization_membership.revoked"} <= set(events(logger))
        role_change = next(fields for event, _, fields in logger.records if event == "organization_membership.role_changed")
        assert role_change["old_role"] == "member" and role_change["new_role"] == "admin"
        assert all("email" not in key and "token" not in key and "hash" not in key for _, _, record in logger.records for key in record)


def test_invitation_events_do_not_capture_email_token_or_hash() -> None:
    with SessionLocal() as db:
        logger = CapturingLogger(); owner = user(db, "invite-owner"); invitee = user(db, "invitee"); second_invitee = user(db, "second-invitee")
        org = Organization(kind="shared", name="Invites", slug=f"{P}{uuid4().hex}")
        db.add(org); db.flush(); db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")); db.commit()
        delivery = FakeEmailDeliveryAdapter(capture_deliveries=True)
        invitation_service = OrganizationInvitationApplicationService(db, PEPPER, delivery, structured_logger=logger)
        issued = invitation_service.issue(owner.id, org.id, invitee.email, "member")
        raw = delivery.deliveries[0].raw_token
        invitation_service.accept(invitee.id, raw)
        revocable = invitation_service.issue(owner.id, org.id, second_invitee.email, "admin")
        invitation_service.revoke(owner.id, org.id, revocable.invitation_id)
        rendered = repr(logger.records)
        assert {"organization_invitation.issued", "organization_invitation.accepted", "organization_invitation.revoked"} <= set(events(logger))
        assert raw not in rendered and invitee.email not in rendered
        assert db.get(OrganizationInvitation, issued.invitation_id).token_hash not in rendered
        assert all("email" not in key and "token" not in key and "hash" not in key for _, _, record in logger.records for key in record)


def test_denied_last_owner_and_cross_tenant_operations_are_diagnostic_without_target_metadata() -> None:
    with SessionLocal() as db:
        logger = CapturingLogger(); creator, metadata, memberships, _ = services(db, logger)
        owner = user(db, "last-owner"); outsider = user(db, "outsider")
        created = creator.create(owner.id, "Denied", f"{P}{uuid4().hex}")
        with pytest.raises(LastOrganizationOwnerError):
            memberships.revoke(owner.id, created.organization_id, created.owner_membership_id)
        with pytest.raises(OrganizationMetadataError):
            metadata.update_name(outsider.id, created.organization_id, "Denied")
        denied = [fields for event, _, fields in logger.records if event == "organization.operation_denied"]
        assert len(denied) == 2
        assert {item["reason_code"] for item in denied} == {"last_owner", "policy_denied"}
        assert all("name" not in fields and "email" not in fields for fields in denied)


def test_structured_logging_failure_is_fail_open_after_organization_commit() -> None:
    class ExplodingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("logging unavailable")

    with SessionLocal() as db:
        sink = logging.getLogger(f"{P}{uuid4().hex}")
        sink.handlers = [ExplodingHandler()]
        sink.propagate = False
        safe_logger = SafeStructuredLogger(sink)
        owner = user(db, "fail-open-owner")
        service = SharedOrganizationApplicationService(
            db, UserRepository(db), OrganizationRepository(db), OrganizationMembershipRepository(db),
            WorkspaceRepository(db), structured_logger=safe_logger,
        )
        result = service.create(owner.id, "Fail Open", f"{P}{uuid4().hex}")
        assert db.get(Organization, result.organization_id) is not None

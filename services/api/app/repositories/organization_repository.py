"""Transaction-neutral persistence access for organizational aggregates."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.organization import Organization, OrganizationMembership, OrganizationStatus


class OrganizationRepository:
    """Stage and query organizations; callers own transaction and authorization policy."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, organization: Organization) -> Organization:
        self._database.add(organization)
        self._database.flush()
        return organization

    def get_by_id(self, organization_id: UUID) -> Organization | None:
        return self._database.get(Organization, organization_id)

    def get_by_id_for_update(self, organization_id: UUID) -> Organization | None:
        return self._database.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )

    def get_personal_by_owner_user_id(self, user_id: UUID) -> Organization | None:
        return self._database.scalar(
            select(Organization).where(Organization.personal_owner_user_id == user_id)
        )

    def get_personal_by_owner_user_id_for_update(self, user_id: UUID) -> Organization | None:
        return self._database.scalar(
            select(Organization)
            .where(Organization.personal_owner_user_id == user_id)
            .with_for_update()
        )

    def list_accessible_by_user(self, user_id: UUID) -> list[Organization]:
        statement = (
            select(Organization)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.revoked_at.is_(None),
            )
            .order_by(Organization.created_at.asc(), Organization.id.asc())
        )
        return list(self._database.scalars(statement))

    def update_name_and_slug(self, organization: Organization, name: str, slug: str) -> None:
        organization.name = name
        organization.slug = slug
        self._database.flush()

    def block(self, organization: Organization) -> None:
        organization.status = OrganizationStatus.BLOCKED.value
        if organization.blocked_at is None:
            organization.blocked_at = datetime.now(UTC)
        self._database.flush()

"""Transaction-neutral, organization-scoped membership persistence."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.organization import OrganizationMembership, OrganizationMembershipRole


class OrganizationMembershipRepository:
    """Read and stage membership state without deciding authorization policy."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, membership: OrganizationMembership) -> OrganizationMembership:
        self._database.add(membership)
        self._database.flush()
        return membership

    def get_by_id_in_organization(
        self, organization_id: UUID, membership_id: UUID
    ) -> OrganizationMembership | None:
        return self._database.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.id == membership_id,
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.revoked_at.is_(None),
            )
        )

    def get_by_id_in_organization_for_update(
        self, organization_id: UUID, membership_id: UUID
    ) -> OrganizationMembership | None:
        return self._database.scalar(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.id == membership_id,
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.revoked_at.is_(None),
            )
            .with_for_update()
        )

    def get_historical_by_id_in_organization(
        self, organization_id: UUID, membership_id: UUID
    ) -> OrganizationMembership | None:
        """Explicitly retrieve a revoked or active historical membership record."""

        return self._database.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.id == membership_id,
                OrganizationMembership.organization_id == organization_id,
            )
        )

    def get_active_for_user(
        self, organization_id: UUID, user_id: UUID
    ) -> OrganizationMembership | None:
        return self._database.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.revoked_at.is_(None),
            )
        )

    def get_active_for_user_for_update(
        self, organization_id: UUID, user_id: UUID
    ) -> OrganizationMembership | None:
        return self._database.scalar(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.revoked_at.is_(None),
            )
            .with_for_update()
        )

    def list_active_for_organization(self, organization_id: UUID) -> list[OrganizationMembership]:
        statement = (
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.revoked_at.is_(None),
            )
            .order_by(OrganizationMembership.created_at.asc(), OrganizationMembership.id.asc())
        )
        return list(self._database.scalars(statement))

    def list_active_owners_for_update(self, organization_id: UUID) -> list[OrganizationMembership]:
        statement = (
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.role == OrganizationMembershipRole.OWNER.value,
                OrganizationMembership.revoked_at.is_(None),
            )
            .order_by(OrganizationMembership.created_at.asc(), OrganizationMembership.id.asc())
            .with_for_update()
        )
        return list(self._database.scalars(statement))

    def count_active_owners(self, organization_id: UUID) -> int:
        return int(
            self._database.scalar(
                select(func.count())
                .select_from(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.role == OrganizationMembershipRole.OWNER.value,
                    OrganizationMembership.revoked_at.is_(None),
                )
            )
            or 0
        )

    def update_role(self, membership: OrganizationMembership, role: str) -> None:
        membership.role = role
        self._database.flush()

    def revoke(self, membership: OrganizationMembership) -> None:
        if membership.revoked_at is None:
            membership.revoked_at = datetime.now(UTC)
            self._database.flush()

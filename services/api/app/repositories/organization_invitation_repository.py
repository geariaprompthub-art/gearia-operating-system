"""Transaction-neutral persistence operations for organization invitations."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.organization import OrganizationInvitation


class OrganizationInvitationRepository:
    """Persist only invitation hashes; token validation belongs to a future service."""

    def __init__(self, database: Session) -> None:
        self._database = database

    def create(self, invitation: OrganizationInvitation) -> OrganizationInvitation:
        self._database.add(invitation)
        self._database.flush()
        return invitation

    def get_by_id_in_organization(
        self, organization_id: UUID, invitation_id: UUID
    ) -> OrganizationInvitation | None:
        return self._database.scalar(
            select(OrganizationInvitation).where(
                OrganizationInvitation.id == invitation_id,
                OrganizationInvitation.organization_id == organization_id,
            )
        )

    def get_active_by_email(
        self, organization_id: UUID, invited_email_normalized: str
    ) -> OrganizationInvitation | None:
        return self._database.scalar(
            select(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.invited_email_normalized == invited_email_normalized,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.invalidated_at.is_(None),
            )
        )

    def get_by_token_hash(self, token_hash: str) -> OrganizationInvitation | None:
        return self._database.scalar(
            select(OrganizationInvitation).where(OrganizationInvitation.token_hash == token_hash)
        )

    def get_by_token_hash_for_update(self, token_hash: str) -> OrganizationInvitation | None:
        return self._database.scalar(
            select(OrganizationInvitation)
            .where(OrganizationInvitation.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def invalidate_active_for_email(self, organization_id: UUID, invited_email_normalized: str) -> int:
        result = self._database.execute(
            update(OrganizationInvitation)
            .where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.invited_email_normalized == invited_email_normalized,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.invalidated_at.is_(None),
            )
            .values(invalidated_at=datetime.now(UTC))
        )
        self._database.flush()
        return int(result.rowcount or 0)

    def mark_accepted(self, invitation: OrganizationInvitation) -> None:
        invitation.accepted_at = datetime.now(UTC)
        self._database.flush()

    def invalidate(self, invitation: OrganizationInvitation) -> None:
        if invitation.invalidated_at is None:
            invitation.invalidated_at = datetime.now(UTC)
            self._database.flush()

    def list_for_organization(self, organization_id: UUID) -> list[OrganizationInvitation]:
        statement = (
            select(OrganizationInvitation)
            .where(OrganizationInvitation.organization_id == organization_id)
            .order_by(OrganizationInvitation.created_at.asc(), OrganizationInvitation.id.asc())
        )
        return list(self._database.scalars(statement))

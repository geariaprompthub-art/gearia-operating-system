"""Immutable, ORM-free authorization context for an active organization."""

from dataclasses import dataclass
from uuid import UUID

from app.models.organization import (
    OrganizationKind,
    OrganizationMembershipRole,
    OrganizationStatus,
)


@dataclass(frozen=True)
class OrganizationContext:
    """Authorization facts for one active membership, with no persistence handles."""

    user_id: UUID
    organization_id: UUID
    organization_kind: OrganizationKind
    organization_status: OrganizationStatus
    membership_id: UUID
    membership_role: OrganizationMembershipRole

    def __post_init__(self) -> None:
        """Keep an authorized context limited to the active organization state."""

        if self.organization_status is not OrganizationStatus.ACTIVE:
            raise ValueError("organization context requires an active organization")

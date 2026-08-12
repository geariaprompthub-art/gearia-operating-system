"""Internal, transactional lifecycle for opaque shared-organization invitations."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.organization import OrganizationInvitation, OrganizationKind, OrganizationMembership, OrganizationMembershipRole, OrganizationStatus
from app.models.user import User, UserStatus
from app.repositories.organization_invitation_repository import OrganizationInvitationRepository
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.services.email_delivery import EmailDeliveryAdapter
from app.services.email_normalization import normalize_email
from app.services.organization_context_resolver import OrganizationContextResolver, OrganizationContextUnavailableError
from app.services.organization_policy import OrganizationAction, OrganizationPolicy


class OrganizationInvitationError(RuntimeError):
    """Sanitized internal invitation failure."""


class OrganizationInvitationAlreadyMemberError(OrganizationInvitationError):
    """The invited identity is already an active member."""


@dataclass(frozen=True)
class OrganizationInvitationIssueResult:
    invitation_id: UUID
    organization_id: UUID
    role: str
    expires_at: datetime
    delivery_failed: bool


@dataclass(frozen=True)
class OrganizationInvitationResult:
    invitation_id: UUID
    organization_id: UUID
    role: str
    accepted_at: datetime | None
    invalidated_at: datetime | None


class OrganizationInvitationApplicationService:
    """Own invitation commits; lock order is organization, actor, invitation, user, membership."""

    def __init__(self, database: Session, pepper: str, email_delivery_adapter: EmailDeliveryAdapter, ttl: timedelta = timedelta(days=7), structured_logger: SafeStructuredLogger | None = None) -> None:
        if not isinstance(pepper, str) or not pepper:
            raise ValueError("lifecycle token pepper is required")
        self._database, self._pepper, self._delivery, self._ttl = database, pepper.encode(), email_delivery_adapter, ttl
        self._organizations = OrganizationRepository(database)
        self._memberships = OrganizationMembershipRepository(database)
        self._invitations = OrganizationInvitationRepository(database)
        self._users = UserRepository(database)
        self._contexts = OrganizationContextResolver(self._organizations, self._memberships)
        self._logger = structured_logger

    def _hash(self, raw: object) -> str:
        if not isinstance(raw, str) or not raw or len(raw) > 256:
            raise OrganizationInvitationError("organization invitation unavailable")
        return hmac.new(self._pepper, f"organization-invitation:{raw}".encode(), hashlib.sha256).hexdigest()

    def _organization(self, organization_id: UUID):
        org = self._organizations.get_by_id_for_update(organization_id)
        if org is None or org.kind != OrganizationKind.SHARED.value or org.status != OrganizationStatus.ACTIVE.value:
            raise OrganizationInvitationError("organization invitation unavailable")
        return org

    def _actor(self, actor_id: UUID, organization_id: UUID, action: OrganizationAction):
        try:
            context = self._contexts.resolve(actor_id, organization_id)
        except OrganizationContextUnavailableError as error:
            raise OrganizationInvitationError("organization invitation unavailable") from error
        if not OrganizationPolicy.is_allowed(context, action):
            raise OrganizationInvitationError("organization invitation unavailable")
        return self._memberships.get_active_for_user_for_update(organization_id, actor_id)

    @staticmethod
    def _result(row: OrganizationInvitation) -> OrganizationInvitationResult:
        return OrganizationInvitationResult(row.id, row.organization_id, row.role, row.accepted_at, row.invalidated_at)

    def issue(self, actor_user_id: UUID, organization_id: UUID, email: str, role: str, correlation_id: str | None = None) -> OrganizationInvitationIssueResult:
        normalized = normalize_email(email)
        if role not in {OrganizationMembershipRole.MEMBER.value, OrganizationMembershipRole.ADMIN.value}:
            raise OrganizationInvitationError("organization invitation unavailable")
        raw = secrets.token_urlsafe(32)
        try:
            self._organization(organization_id); self._actor(actor_user_id, organization_id, OrganizationAction.INVITE_MEMBER)
            existing_user = self._users.get_by_normalized_email(normalized.normalized)
            if existing_user and self._memberships.get_active_for_user_for_update(organization_id, existing_user.id):
                raise OrganizationInvitationAlreadyMemberError("organization invitation unavailable")
            previous = self._invitations.invalidate_active_for_email(organization_id, normalized.normalized)
            row = self._invitations.create(OrganizationInvitation(organization_id=organization_id, invited_email_normalized=normalized.normalized, role=role, token_hash=self._hash(raw), expires_at=datetime.now(UTC) + self._ttl, created_by_membership_id=self._memberships.get_active_for_user(organization_id, actor_user_id).id))
            self._database.commit()
            if self._logger:
                event = LogEvent.ORGANIZATION_INVITATION_REISSUED if previous else LogEvent.ORGANIZATION_INVITATION_ISSUED
                self._logger.info(event, "Organization invitation issued", actor_user_id=str(actor_user_id), organization_id=str(organization_id), invitation_id=str(row.id), role=role, result="success")
        except Exception:
            self._database.rollback(); raise
        failed = False
        try: self._delivery.send_organization_invitation(normalized.email, raw, correlation_id)
        except Exception: failed = True
        return OrganizationInvitationIssueResult(row.id, row.organization_id, row.role, row.expires_at, failed)

    def accept(self, actor_user_id: UUID, raw_token: str) -> OrganizationInvitationResult:
        token_hash = self._hash(raw_token)
        try:
            probe = self._invitations.get_by_token_hash(token_hash)
            if probe is None:
                raise OrganizationInvitationError("organization invitation unavailable")
            self._organization(probe.organization_id)
            row = self._invitations.get_by_token_hash_for_update(token_hash)
            user = self._users.get_by_id_for_update(actor_user_id)
            now = datetime.now(UTC)
            if row is None or row.accepted_at or row.invalidated_at or row.expires_at <= now or user is None or user.status != UserStatus.ACTIVE.value or user.email_normalized != row.invited_email_normalized:
                raise OrganizationInvitationError("organization invitation unavailable")
            if self._memberships.get_active_for_user_for_update(row.organization_id, user.id):
                raise OrganizationInvitationAlreadyMemberError("organization invitation unavailable")
            self._memberships.create(OrganizationMembership(organization_id=row.organization_id, user_id=user.id, role=row.role))
            self._invitations.mark_accepted(row); self._database.commit()
            if self._logger:
                self._logger.info(LogEvent.ORGANIZATION_INVITATION_ACCEPTED, "Organization invitation accepted", actor_user_id=str(actor_user_id), organization_id=str(row.organization_id), invitation_id=str(row.id), role=row.role, result="success")
            return self._result(row)
        except Exception:
            self._database.rollback(); raise

    def revoke(self, actor_user_id: UUID, organization_id: UUID, invitation_id: UUID) -> OrganizationInvitationResult:
        try:
            self._organization(organization_id); self._actor(actor_user_id, organization_id, OrganizationAction.INVALIDATE_INVITATION)
            row = self._invitations.get_by_id_in_organization(organization_id, invitation_id)
            if row is None or row.accepted_at or row.invalidated_at or row.expires_at <= datetime.now(UTC): raise OrganizationInvitationError("organization invitation unavailable")
            self._invitations.invalidate(row); self._database.commit()
            if self._logger:
                self._logger.info(LogEvent.ORGANIZATION_INVITATION_REVOKED, "Organization invitation revoked", actor_user_id=str(actor_user_id), organization_id=str(organization_id), invitation_id=str(row.id), result="success")
            return self._result(row)
        except Exception:
            self._database.rollback(); raise

"""Internal transactional creation and read contracts for shared organizations."""

from dataclasses import dataclass
from datetime import datetime
import re
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger
from app.models.organization import (
    Organization,
    OrganizationKind,
    OrganizationMembership,
    OrganizationMembershipRole,
    OrganizationStatus,
)
from app.models.user import UserStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.organization_context_resolver import (
    OrganizationContextResolver,
    OrganizationContextUnavailableError,
)


class SharedOrganizationError(RuntimeError):
    """Sanitized internal failure for unavailable shared-organization operations."""


class SharedOrganizationSlugConflictError(SharedOrganizationError):
    """A shared organization slug is already reserved."""


@dataclass(frozen=True)
class SharedOrganizationResult:
    """ORM-free identifiers and display data returned after one committed creation."""

    organization_id: UUID
    owner_membership_id: UUID
    workspace_id: UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AccessibleOrganization:
    """Minimal internal organization read model with no user PII."""

    organization_id: UUID
    kind: str
    name: str
    slug: str
    membership_id: UUID
    membership_role: str


@dataclass(frozen=True)
class OrganizationWorkspace:
    """Minimal internal workspace read model for an authorized organization."""

    workspace_id: UUID
    organization_id: UUID
    name: str
    status: str


class SharedOrganizationApplicationService:
    """Own the transaction that creates a shared aggregate and its initial workspace."""

    _SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    _MAX_NAME_LENGTH = 120
    _MAX_SLUG_LENGTH = 120
    _INITIAL_WORKSPACE_NAME = "General"

    def __init__(
        self,
        database: Session,
        user_repository: UserRepository | None = None,
        organization_repository: OrganizationRepository | None = None,
        membership_repository: OrganizationMembershipRepository | None = None,
        workspace_repository: WorkspaceRepository | None = None,
        context_resolver: OrganizationContextResolver | None = None,
        structured_logger: SafeStructuredLogger | None = None,
    ) -> None:
        self._database = database
        self._users = user_repository or UserRepository(database)
        self._organizations = organization_repository or OrganizationRepository(database)
        self._memberships = membership_repository or OrganizationMembershipRepository(database)
        self._workspaces = workspace_repository or WorkspaceRepository(database)
        self._contexts = context_resolver or OrganizationContextResolver(
            self._organizations, self._memberships
        )
        self._logger = structured_logger

    @classmethod
    def _normalize_name(cls, value: object) -> str:
        if not isinstance(value, str):
            raise SharedOrganizationError("shared organization unavailable")
        normalized = value.strip()
        if not normalized or len(normalized) > cls._MAX_NAME_LENGTH:
            raise SharedOrganizationError("shared organization unavailable")
        return normalized

    @classmethod
    def _normalize_slug(cls, value: object) -> str:
        if not isinstance(value, str):
            raise SharedOrganizationError("shared organization unavailable")
        normalized = re.sub(r"-+", "-", value.strip().lower())
        if (
            not normalized
            or len(normalized) > cls._MAX_SLUG_LENGTH
            or "@" in normalized
            or cls._SLUG.fullmatch(normalized) is None
        ):
            raise SharedOrganizationError("shared organization unavailable")
        return normalized

    @staticmethod
    def _validate_result(
        organization: Organization, membership: OrganizationMembership, workspace: Workspace, actor_user_id: UUID
    ) -> None:
        if (
            organization.kind != OrganizationKind.SHARED.value
            or organization.status != OrganizationStatus.ACTIVE.value
            or organization.personal_owner_user_id is not None
            or membership.organization_id != organization.id
            or membership.user_id != actor_user_id
            or membership.role != OrganizationMembershipRole.OWNER.value
            or membership.revoked_at is not None
            or workspace.organization_id != organization.id
            or workspace.owner_user_id is not None
            or workspace.status != WorkspaceStatus.ACTIVE.value
        ):
            raise SharedOrganizationError("shared organization unavailable")

    def create(self, actor_user_id: UUID, name: str, slug: str) -> SharedOrganizationResult:
        """Create the shared aggregate atomically; no idempotency is implied by slug."""

        if not isinstance(actor_user_id, UUID):
            raise SharedOrganizationError("shared organization unavailable")
        normalized_name = self._normalize_name(name)
        normalized_slug = self._normalize_slug(slug)
        try:
            actor = self._users.get_by_id_for_update(actor_user_id)
            if actor is None or actor.status != UserStatus.ACTIVE.value:
                raise SharedOrganizationError("shared organization unavailable")
            organization = self._organizations.create(
                Organization(
                    kind=OrganizationKind.SHARED.value,
                    name=normalized_name,
                    slug=normalized_slug,
                    status=OrganizationStatus.ACTIVE.value,
                    personal_owner_user_id=None,
                )
            )
            membership = self._memberships.create(
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=actor_user_id,
                    role=OrganizationMembershipRole.OWNER.value,
                )
            )
            workspace = self._workspaces.create(
                Workspace(
                    organization_id=organization.id,
                    owner_user_id=None,
                    name=self._INITIAL_WORKSPACE_NAME,
                    status=WorkspaceStatus.ACTIVE.value,
                )
            )
            self._validate_result(organization, membership, workspace, actor_user_id)
            self._database.commit()
            if self._logger:
                self._logger.info(LogEvent.ORGANIZATION_CREATED, "Organization created", actor_user_id=str(actor_user_id), organization_id=str(organization.id), workspace_id=str(workspace.id), membership_id=str(membership.id), result="success")
            return SharedOrganizationResult(
                organization_id=organization.id,
                owner_membership_id=membership.id,
                workspace_id=workspace.id,
                name=organization.name,
                slug=organization.slug,
                created_at=organization.created_at,
                updated_at=organization.updated_at,
            )
        except IntegrityError as error:
            self._database.rollback()
            raise SharedOrganizationSlugConflictError("shared organization unavailable") from error
        except Exception:
            self._database.rollback()
            raise

    def list_accessible(self, actor_user_id: UUID) -> list[AccessibleOrganization]:
        """List active memberships only, with a stable repository order."""

        if not isinstance(actor_user_id, UUID):
            raise SharedOrganizationError("shared organization unavailable")
        organizations: list[AccessibleOrganization] = []
        for organization in self._organizations.list_accessible_by_user(actor_user_id):
            try:
                context = self._contexts.resolve(actor_user_id, organization.id)
            except OrganizationContextUnavailableError:
                continue
            organizations.append(
                AccessibleOrganization(
                    organization_id=organization.id,
                    kind=context.organization_kind.value,
                    name=organization.name,
                    slug=organization.slug,
                    membership_id=context.membership_id,
                    membership_role=context.membership_role.value,
                )
            )
        return organizations

    def get_shared_for_user(self, actor_user_id: UUID, organization_id: UUID) -> AccessibleOrganization:
        """Return only an accessible active shared organization; blocked or revoked access fails closed."""

        try:
            context = self._contexts.resolve(actor_user_id, organization_id)
        except (OrganizationContextUnavailableError, TypeError, ValueError) as error:
            raise SharedOrganizationError("shared organization unavailable") from error
        organization = self._organizations.get_by_id(organization_id)
        if organization is None or context.organization_kind is not OrganizationKind.SHARED:
            raise SharedOrganizationError("shared organization unavailable")
        return AccessibleOrganization(
            organization_id=organization.id,
            kind=context.organization_kind.value,
            name=organization.name,
            slug=organization.slug,
            membership_id=context.membership_id,
            membership_role=context.membership_role.value,
        )

    def list_workspaces(self, actor_user_id: UUID, organization_id: UUID) -> list[OrganizationWorkspace]:
        """Read organization workspaces only after active membership resolution."""

        self.get_shared_for_user(actor_user_id, organization_id)
        return [
            OrganizationWorkspace(
                workspace_id=workspace.id,
                organization_id=workspace.organization_id,
                name=workspace.name,
                status=workspace.status,
            )
            for workspace in self._workspaces.list_by_organization_id(organization_id)
        ]

"""Explicit tenancy contexts shared by synchronous and asynchronous workflows."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True)
class WorkspaceContext:
    """Mandatory authorization boundary for access to workspace-private resources."""

    workspace_id: UUID
    user_id: UUID


@dataclass(frozen=True)
class WorkspaceExecutionContext:
    """Immutable execution metadata required by every workspace-scoped background job."""

    workspace_id: UUID
    correlation_id: UUID
    actor_type: str
    actor_id: UUID | None
    trigger: str
    timestamp: datetime

    def __post_init__(self) -> None:
        """Reject incomplete execution context before a job can run."""

        if not self.actor_type or self.actor_type != self.actor_type.strip():
            raise ValueError("actor_type must be a non-empty canonical string")
        if not self.trigger or self.trigger != self.trigger.strip():
            raise ValueError("trigger must be a non-empty canonical string")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")

    @classmethod
    def now(
        cls,
        *,
        workspace_id: UUID,
        correlation_id: UUID,
        actor_type: str,
        actor_id: UUID | None,
        trigger: str,
    ) -> "WorkspaceExecutionContext":
        """Build a timestamped context without allowing implicit workspace selection."""

        return cls(
            workspace_id=workspace_id,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            trigger=trigger,
            timestamp=datetime.now(UTC),
        )

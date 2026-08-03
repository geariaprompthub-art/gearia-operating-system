"""Internal-only email delivery seam for lifecycle notifications."""

from dataclasses import dataclass, field
from typing import Protocol


class EmailDeliveryAdapter(Protocol):
    """Deliver verification material only after its owning transaction commits."""

    def send_email_verification(
        self,
        recipient: str,
        raw_token: str,
        correlation_id: str | None,
    ) -> None:
        """Request delivery without persisting or logging token material."""

    def send_password_reset(
        self,
        recipient: str,
        raw_token: str,
        correlation_id: str | None,
    ) -> None:
        """Request reset delivery without persisting or logging token material."""


@dataclass(frozen=True)
class FakeEmailDelivery:
    """Test-only capture whose sensitive token field is excluded from repr."""

    recipient: str
    raw_token: str = field(repr=False)
    correlation_id: str | None
    template: str = "email_verification"


class FakeEmailDeliveryAdapter:
    """Deterministic non-network adapter; capture is opt-in for tests only."""

    def __init__(self, *, capture_deliveries: bool = False, fail: bool = False) -> None:
        self._capture_deliveries = capture_deliveries
        self._fail = fail
        self.call_count = 0
        self.deliveries: list[FakeEmailDelivery] = []

    def send_email_verification(
        self,
        recipient: str,
        raw_token: str,
        correlation_id: str | None,
    ) -> None:
        self.call_count += 1
        if self._fail:
            raise RuntimeError("controlled email delivery failure")
        if self._capture_deliveries:
            self.deliveries.append(FakeEmailDelivery(recipient, raw_token, correlation_id))

    def send_password_reset(
        self,
        recipient: str,
        raw_token: str,
        correlation_id: str | None,
    ) -> None:
        self.call_count += 1
        if self._fail:
            raise RuntimeError("controlled email delivery failure")
        if self._capture_deliveries:
            self.deliveries.append(
                FakeEmailDelivery(recipient, raw_token, correlation_id, "password_reset")
            )

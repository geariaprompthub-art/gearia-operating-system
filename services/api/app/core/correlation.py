"""Request correlation context independent from the HTTP framework."""

import re
from contextvars import ContextVar, Token
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class CorrelationContext:
    """Expose the current request identifier without retaining HTTP objects."""

    def get(self) -> str | None:
        return _request_id.get()

    def set(self, request_id: str) -> Token[str | None]:
        return _request_id.set(request_id)

    def reset(self, token: Token[str | None]) -> None:
        _request_id.reset(token)


correlation_context = CorrelationContext()


def resolve_request_id(value: str | None) -> str:
    """Preserve only a bounded safe identifier, otherwise generate a UUID."""

    return value if value is not None and _REQUEST_ID_PATTERN.fullmatch(value) else str(uuid4())

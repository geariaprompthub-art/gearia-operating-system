"""ASGI-safe request correlation and minimal HTTP structured events."""

from time import perf_counter

from fastapi import Request

from app.core.correlation import REQUEST_ID_HEADER, correlation_context, resolve_request_id
from app.core.log_events import LogEvent
from app.core.structured_logging import SafeStructuredLogger


def install_request_correlation(app: object, logger: SafeStructuredLogger) -> None:
    """Attach a small HTTP middleware without changing response payloads."""

    @app.middleware("http")  # type: ignore[attr-defined]
    async def request_correlation(request: Request, call_next: object):
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = correlation_context.set(request_id)
        started = perf_counter()
        route = request.url.path
        logger.info(LogEvent.HTTP_REQUEST_STARTED, "HTTP request started", method=request.method, route=route)
        try:
            response = await call_next(request)  # type: ignore[operator]
            response.headers[REQUEST_ID_HEADER] = request_id
            duration_ms = (perf_counter() - started) * 1000
            if response.status_code >= 500:
                logger.error(LogEvent.HTTP_REQUEST_FAILED, "HTTP request failed", method=request.method, route=route, http_status=response.status_code, duration_ms=duration_ms)
            else:
                logger.info(LogEvent.HTTP_REQUEST_COMPLETED, "HTTP request completed", method=request.method, route=route, http_status=response.status_code, duration_ms=duration_ms)
            return response
        except Exception as error:
            logger.error(LogEvent.HTTP_REQUEST_FAILED, "HTTP request failed", method=request.method, route=route, error_type=type(error).__name__, duration_ms=(perf_counter() - started) * 1000)
            raise
        finally:
            correlation_context.reset(token)

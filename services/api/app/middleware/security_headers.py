"""Response-only HTTP hardening that does not inspect request payloads."""

from fastapi import Request


def install_security_headers(app: object) -> None:
    """Attach stable browser-facing security headers to every HTTP response."""

    @app.middleware("http")  # type: ignore[attr-defined]
    async def security_headers(request: Request, call_next: object):
        response = await call_next(request)  # type: ignore[operator]
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith(("/embeddings", "/search")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

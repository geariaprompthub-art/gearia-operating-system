"""Central HTTP cookie policy for future authentication endpoints."""

from datetime import UTC, datetime, timedelta

from fastapi import Response


class CookiePolicy:
    """Keep cookie attributes identical when issuing and clearing cookies."""

    access_name = "gearia_access"
    refresh_name = "gearia_refresh"
    csrf_name = "gearia_csrf"

    def __init__(self, secure: bool, samesite: str, domain: str | None, access_ttl: int, refresh_ttl: int) -> None:
        if samesite not in {"lax", "strict", "none"}:
            raise ValueError("invalid cookie SameSite policy")
        if access_ttl < 1 or refresh_ttl < 1:
            raise ValueError("invalid cookie TTL")
        if domain is not None and (not domain.strip() or any(character.isspace() for character in domain)):
            raise ValueError("invalid cookie domain")
        self._secure, self._samesite, self._domain = secure, samesite, domain
        self._access_ttl, self._refresh_ttl = access_ttl, refresh_ttl

    def set_tokens(self, response: Response, access: str, refresh: str, csrf: str) -> None:
        now = datetime.now(UTC)
        response.set_cookie(self.access_name, access, max_age=self._access_ttl, expires=now + timedelta(seconds=self._access_ttl), httponly=True, secure=self._secure, samesite=self._samesite, path="/", domain=self._domain)
        response.set_cookie(self.refresh_name, refresh, max_age=self._refresh_ttl, expires=now + timedelta(seconds=self._refresh_ttl), httponly=True, secure=self._secure, samesite=self._samesite, path="/auth", domain=self._domain)
        response.set_cookie(self.csrf_name, csrf, max_age=self._refresh_ttl, expires=now + timedelta(seconds=self._refresh_ttl), httponly=False, secure=self._secure, samesite=self._samesite, path="/auth", domain=self._domain)

    def clear(self, response: Response) -> None:
        for name, path in ((self.access_name, "/"), (self.refresh_name, "/auth"), (self.csrf_name, "/auth")):
            response.delete_cookie(name, path=path, domain=self._domain, secure=self._secure, samesite=self._samesite)

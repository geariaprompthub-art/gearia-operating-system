"""Centralized email normalization for identity operations."""

import unicodedata
from dataclasses import dataclass
from email.utils import parseaddr


class InvalidEmailError(ValueError):
    """Raised for an internal email input that cannot identify a user."""


@dataclass(frozen=True)
class NormalizedEmail:
    email: str
    normalized: str


def normalize_email(value: object) -> NormalizedEmail:
    """Trim, NFC-normalize and casefold an email without provider-specific rewriting."""

    if not isinstance(value, str):
        raise InvalidEmailError("email must be a string")
    email = unicodedata.normalize("NFC", value.strip())
    if not email or len(email) > 320 or parseaddr(email)[1] != email or email.count("@") != 1:
        raise InvalidEmailError("email is invalid")
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise InvalidEmailError("email is invalid")
    return NormalizedEmail(email=email, normalized=email.casefold())

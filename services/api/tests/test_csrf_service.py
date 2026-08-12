"""Pure contracts for the reusable double-submit CSRF primitive."""

import pytest

from app.services.csrf_service import CsrfService


@pytest.mark.parametrize("cookie,header", [
    (None, "token"), ("token", None), (None, None), ("", "token"),
    ("token", ""), ("token", "different"), ("token", "tokenx"),
])
def test_valid_pair_fails_closed_for_missing_or_mismatched_values(cookie, header):
    csrf = CsrfService()
    assert csrf.valid_pair(cookie, header, csrf.hash("token")) is False


def test_valid_pair_accepts_the_same_long_value_without_storing_it():
    csrf = CsrfService()
    token = "x" * 200
    assert csrf.valid_pair(token, token, csrf.hash(token)) is True

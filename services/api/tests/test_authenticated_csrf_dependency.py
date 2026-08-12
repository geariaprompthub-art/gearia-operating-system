"""Adversarial PostgreSQL contracts for session-bound CSRF dependency."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.auth_session import AuthSession
from app.models.user import User
from app.repositories.auth_session_repository import AuthSessionRepository
from app.services.access_token_authenticator import AuthenticatedPrincipal
from app.services.csrf_service import CsrfService
from app.services.principal_dependencies import require_authenticated_csrf

P = "csrf-dependency-"

@pytest.fixture(autouse=True)
def cleanup():
    def clear(db):
        ids = list(db.scalars(select(User.id).where(User.email_normalized.like(f"{P}%"))))
        db.execute(delete(AuthSession).where(AuthSession.user_id.in_(ids))); db.execute(delete(User).where(User.id.in_(ids))); db.commit()
    with SessionLocal() as db: clear(db)
    yield
    with SessionLocal() as db: clear(db)

def make(db, label, csrf):
    email=f"{P}{label}-{uuid4().hex}@test"; user=User(email=email,email_normalized=email,password_hash="h",status="active"); db.add(user); db.flush()
    token, hashed=csrf.issue(); session=AuthSession(user_id=user.id,token_version=1,csrf_secret_hash=hashed,expires_at=datetime.now(UTC)+timedelta(hours=1)); db.add(session); db.commit()
    principal=AuthenticatedPrincipal(user.id,session.id,1,uuid4(),datetime.now(UTC),datetime.now(UTC)+timedelta(hours=1),user.email,user.status,None,user.created_at)
    return user,session,principal,token

def test_session_swap_is_rejected_before_csrf_validation():
    with SessionLocal() as db:
        csrf=CsrfService(); user_a, _, principal_a, token_a=make(db,"a",csrf); _, session_b, _, token_b=make(db,"b",csrf)
        assert AuthSessionRepository(db).get_active_for_principal(session_b.id,user_a.id) is None
        swapped=AuthenticatedPrincipal(user_a.id,session_b.id,1,principal_a.token_jti,principal_a.issued_at,principal_a.expires_at,principal_a.email,principal_a.user_status,None,principal_a.created_at)
        with pytest.raises(HTTPException, match="Invalid CSRF token") as error: require_authenticated_csrf(swapped,token_b,token_b,db,csrf)
        assert error.value.status_code == 403

def test_csrf_is_bound_to_each_session_and_revocation_fails_closed():
    with SessionLocal() as db:
        csrf=CsrfService(); _, session_a, principal_a, token_a=make(db,"a",csrf)
        session_b=AuthSession(user_id=principal_a.user_id,token_version=1,csrf_secret_hash=csrf.hash("other"),expires_at=datetime.now(UTC)+timedelta(hours=1)); db.add(session_b); db.commit()
        principal_b=AuthenticatedPrincipal(principal_a.user_id,session_b.id,1,uuid4(),principal_a.issued_at,principal_a.expires_at,principal_a.email,principal_a.user_status,None,principal_a.created_at)
        require_authenticated_csrf(principal_a,token_a,token_a,db,csrf)
        with pytest.raises(HTTPException): require_authenticated_csrf(principal_a,"other","other",db,csrf)
        require_authenticated_csrf(principal_b,"other","other",db,csrf)
        AuthSessionRepository(db).revoke(session_a,"test"); db.commit()
        with pytest.raises(HTTPException) as error: require_authenticated_csrf(principal_a,token_a,token_a,db,csrf)
        assert error.value.status_code == 403

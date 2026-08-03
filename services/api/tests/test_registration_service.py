"""Atomic P2B registration contracts using a real PostgreSQL transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.lifecycle_tokens import EmailVerificationToken
from app.models.user import User
from app.models.workspace import Workspace
from app.services.identity_service import IdentityService
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.password_hasher import PasswordHashingService
from app.services.registration_service import RegistrationService
from app.services.workspace_service import WorkspaceService
from app.repositories.registration_coordination_repository import RegistrationCoordinationRepository


def test_registration_commits_user_workspace_and_hash_only_token_atomically() -> None:
    database = SessionLocal()
    try:
        service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), WorkspaceService(database), LifecycleTokenService(database, "test-pepper"))
        result = service.register("registration@example.com", "valid password")
        assert result.registration_state == "created"
        assert result.raw_verification_token not in repr(database.get(User, result.user_id))
        assert database.get(Workspace, result.workspace_id).owner_user_id == result.user_id
        token = database.scalar(select(EmailVerificationToken).where(EmailVerificationToken.user_id == result.user_id))
        assert token is not None and token.token_hash != result.raw_verification_token
    finally:
        database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == result.user_id)); database.execute(delete(Workspace).where(Workspace.owner_user_id == result.user_id)); database.execute(delete(User).where(User.id == result.user_id)); database.commit(); database.close()


def _service(database):
    return RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), WorkspaceService(database), LifecycleTokenService(database, "test-pepper"))


def test_advisory_key_is_stable_namespaced_and_signed_bigint() -> None:
    first = RegistrationCoordinationRepository.advisory_key("user@example.com")
    assert first == RegistrationCoordinationRepository.advisory_key("user@example.com")
    assert first != RegistrationCoordinationRepository.advisory_key("other@example.com")
    assert -(2**63) <= first <= 2**63 - 1


def test_two_real_postgresql_sessions_converge_first_registration() -> None:
    email = "concurrent-registration@example.com"; barrier = Barrier(2)
    results = []
    try:
        def register():
            database = SessionLocal()
            try:
                barrier.wait(timeout=10)
                result = _service(database).register(email, "valid password")
                return result.user_id, result.workspace_id
            finally:
                database.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: register(), range(2)))
        assert results[0] == results[1]
        with SessionLocal() as database:
            user_id, workspace_id = results[0]
            assert database.get(User, user_id) is not None
            assert database.get(Workspace, workspace_id) is not None
            tokens = list(database.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)))
            assert sum(token.invalidated_at is None and token.used_at is None for token in tokens) == 1
    finally:
        if results:
            with SessionLocal() as database:
                user_id, _ = results[0]
                database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)); database.execute(delete(Workspace).where(Workspace.owner_user_id == user_id)); database.execute(delete(User).where(User.id == user_id)); database.commit()


def test_two_real_postgresql_sessions_converge_pending_reissuance() -> None:
    database = SessionLocal(); seed = _service(database).register("pending-registration@example.com", "valid password"); database.close()
    barrier = Barrier(2); results = []
    try:
        def reissue():
            session = SessionLocal()
            try:
                barrier.wait(timeout=10)
                result = _service(session).register("pending-registration@example.com", "valid password")
                return result.user_id, result.workspace_id
            finally:
                session.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reissue(), range(2)))
        assert results == [(seed.user_id, seed.workspace_id)] * 2
        with SessionLocal() as session:
            tokens = list(session.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == seed.user_id)))
            assert len(tokens) == 3
            assert sum(token.invalidated_at is None and token.used_at is None for token in tokens) == 1
    finally:
        with SessionLocal() as session:
            session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == seed.user_id)); session.execute(delete(Workspace).where(Workspace.owner_user_id == seed.user_id)); session.execute(delete(User).where(User.id == seed.user_id)); session.commit()


def test_workspace_failure_rolls_back_new_user_and_registration_state(monkeypatch) -> None:
    database = SessionLocal(); email = "rollback-registration@example.com"
    workspace = WorkspaceService(database)
    monkeypatch.setattr(workspace, "get_or_provision_personal_workspace", lambda _: (_ for _ in ()).throw(RuntimeError("controlled failure")))
    service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), workspace, LifecycleTokenService(database, "test-pepper"))
    try:
        import pytest
        with pytest.raises(RuntimeError, match="controlled failure"):
            service.register(email, "valid password")
        assert database.scalar(select(User).where(User.email_normalized == email)) is None
        assert database.is_active
    finally:
        database.rollback(); database.close()


def test_failure_immediately_before_commit_rolls_back_flushed_user_workspace_and_token(monkeypatch) -> None:
    database = SessionLocal(); email = "rollback-before-commit@example.com"
    service = _service(database)
    commit_calls = []; rollback_calls = []
    original_rollback = database.rollback
    monkeypatch.setattr(database, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failure")))
    monkeypatch.setattr(database, "rollback", lambda: (rollback_calls.append(True), original_rollback())[1])
    try:
        import pytest
        with pytest.raises(RuntimeError, match="commit failure"):
            service.register(email, "valid password")
        assert len(rollback_calls) == 1
        assert database.scalar(select(User).where(User.email_normalized == email)) is None
        assert database.is_active
    finally:
        database.rollback(); database.close()


def test_token_public_contract_failure_rolls_back_after_workspace(monkeypatch) -> None:
    database = SessionLocal(); email = "rollback-after-workspace@example.com"; tokens = LifecycleTokenService(database, "test-pepper")
    monkeypatch.setattr(tokens, "issue_email_verification", lambda *_: (_ for _ in ()).throw(RuntimeError("token failure")))
    service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), WorkspaceService(database), tokens)
    try:
        import pytest
        with pytest.raises(RuntimeError, match="token failure"):
            service.register(email, "valid password")
        assert database.scalar(select(User).where(User.email_normalized == email)) is None
        assert database.is_active
    finally:
        database.rollback(); database.close()


def test_reissuance_failure_after_token_flush_restores_previous_active_token(monkeypatch) -> None:
    database = SessionLocal(); seed = _service(database).register("reissue-rollback@example.com", "valid password")
    tokens = LifecycleTokenService(database, "test-pepper"); original = tokens.issue_email_verification
    def fail_after_issue(*args):
        original(*args)
        raise RuntimeError("after token flush")
    monkeypatch.setattr(tokens, "issue_email_verification", fail_after_issue)
    service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), WorkspaceService(database), tokens)
    try:
        import pytest
        with pytest.raises(RuntimeError, match="after token flush"):
            service.register("reissue-rollback@example.com", "valid password")
        rows = list(database.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == seed.user_id)))
        assert len(rows) == 1 and rows[0].invalidated_at is None
        assert database.get(Workspace, seed.workspace_id) is not None and database.is_active
    finally:
        database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == seed.user_id)); database.execute(delete(Workspace).where(Workspace.owner_user_id == seed.user_id)); database.execute(delete(User).where(User.id == seed.user_id)); database.commit(); database.close()

"""Atomic P2B registration contracts using a real PostgreSQL transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models.lifecycle_tokens import EmailVerificationToken
from app.models.user import User
from app.models.workspace import Workspace
from app.models.organization import Organization, OrganizationInvitation, OrganizationMembership
from app.services.identity_service import IdentityService
from app.services.lifecycle_token_service import LifecycleTokenService
from app.services.password_hasher import PasswordHashingService
from app.services.registration_service import RegistrationService
from app.repositories.registration_coordination_repository import RegistrationCoordinationRepository
from app.repositories.organization_membership_repository import OrganizationMembershipRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.personal_organization_provisioning_service import PersonalOrganizationProvisioningService


def _provisioning(database):
    return PersonalOrganizationProvisioningService(
        OrganizationRepository(database), OrganizationMembershipRepository(database), WorkspaceRepository(database)
    )


def _cleanup_user(database, user_id) -> None:
    organization_ids = select(Organization.id).where(Organization.personal_owner_user_id == user_id)
    membership_ids = select(OrganizationMembership.id).where(
        OrganizationMembership.organization_id.in_(organization_ids)
    )
    database.execute(delete(OrganizationInvitation).where(OrganizationInvitation.organization_id.in_(organization_ids)))
    database.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id))
    database.execute(delete(Workspace).where(Workspace.owner_user_id == user_id))
    database.execute(delete(OrganizationMembership).where(OrganizationMembership.id.in_(membership_ids)))
    database.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
    database.execute(delete(User).where(User.id == user_id))


def test_registration_commits_user_workspace_and_hash_only_token_atomically() -> None:
    database = SessionLocal()
    try:
        service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), _provisioning(database), LifecycleTokenService(database, "test-pepper"))
        result = service.register("registration@example.com", "valid password")
        assert result.registration_state == "created"
        assert result.raw_verification_token not in repr(database.get(User, result.user_id))
        workspace = database.get(Workspace, result.workspace_id)
        assert workspace.owner_user_id == result.user_id and workspace.organization_id is not None
        organization = database.get(Organization, workspace.organization_id)
        assert organization is not None and organization.personal_owner_user_id == result.user_id
        owner = database.scalar(select(OrganizationMembership).where(OrganizationMembership.organization_id == organization.id))
        assert owner is not None and owner.user_id == result.user_id and owner.role == "owner"
        token = database.scalar(select(EmailVerificationToken).where(EmailVerificationToken.user_id == result.user_id))
        assert token is not None and token.token_hash != result.raw_verification_token
    finally:
        _cleanup_user(database, result.user_id); database.commit(); database.close()


def _service(database):
    return RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), _provisioning(database), LifecycleTokenService(database, "test-pepper"))


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
            assert database.scalar(select(Organization).where(Organization.personal_owner_user_id == user_id)) is not None
            assert database.scalar(select(OrganizationMembership).where(OrganizationMembership.user_id == user_id, OrganizationMembership.revoked_at.is_(None))) is not None
            tokens = list(database.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)))
            assert sum(token.invalidated_at is None and token.used_at is None for token in tokens) == 1
    finally:
        if results:
            with SessionLocal() as database:
                user_id, _ = results[0]
                _cleanup_user(database, user_id); database.commit()


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
            assert database.scalar(select(Organization).where(Organization.personal_owner_user_id == seed.user_id)) is not None
    finally:
        with SessionLocal() as session:
            _cleanup_user(session, seed.user_id); session.commit()


def test_workspace_failure_rolls_back_new_user_and_registration_state(monkeypatch) -> None:
    database = SessionLocal(); email = "rollback-registration@example.com"
    provisioning = _provisioning(database)
    monkeypatch.setattr(provisioning, "provision_for_user", lambda _: (_ for _ in ()).throw(RuntimeError("controlled failure")))
    service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), provisioning, LifecycleTokenService(database, "test-pepper"))
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
    service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), _provisioning(database), tokens)
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
    service = RegistrationService(database, IdentityService(database, PasswordHashingService(time_cost=1, memory_cost=1024, parallelism=1)), _provisioning(database), tokens)
    try:
        import pytest
        with pytest.raises(RuntimeError, match="after token flush"):
            service.register("reissue-rollback@example.com", "valid password")
        rows = list(database.scalars(select(EmailVerificationToken).where(EmailVerificationToken.user_id == seed.user_id)))
        assert len(rows) == 1 and rows[0].invalidated_at is None
        assert database.get(Workspace, seed.workspace_id) is not None and database.is_active
    finally:
        _cleanup_user(database, seed.user_id); database.commit(); database.close()

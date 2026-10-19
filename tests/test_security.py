import json
import os
import stat
from dataclasses import FrozenInstanceError

import pytest

from dishell.approval import ApprovalService
from dishell.audit import AuditError, AuditLog
from dishell.permissions import Permission, PermissionDenied, RemoteContext, SecurityPolicy

OWNER = RemoteContext(42, True)


@pytest.fixture
def security(tmp_path):
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    policy = SecurityPolicy(42, audit)
    policy.register("codex-a")
    policy.register("codex-b")
    clock = [0.0]
    approval = ApprovalService(policy, audit, clock=lambda: clock[0])
    return policy, approval, audit, clock


def test_owner_dm_scope_and_permission_checks_fail_closed(security):
    policy, _, _, _ = security
    for ctx in (RemoteContext(9, True), RemoteContext(42, False)):
        with pytest.raises(PermissionDenied):
            policy.check(ctx, "codex-a")
    with pytest.raises(PermissionDenied):
        policy.check(OWNER, "unknown")
    for level in (Permission.INPUT, Permission.MANAGE):
        with pytest.raises(PermissionDenied, match="confirmation"):
            policy.check(OWNER, "codex-a", permission=level)


def test_confirmation_is_immutable_session_bound_and_single_use(security):
    _, approvals, _, _ = security
    request = approvals.request(OWNER, "codex-a", "input", b"fixed\r")
    with pytest.raises(FrozenInstanceError):
        request.payload = b"tampered\r"
    with pytest.raises(PermissionDenied):
        approvals.decide(OWNER, request.nonce, "codex-b", approve=True)
    with pytest.raises(PermissionDenied):
        approvals.decide(RemoteContext(9, True), request.nonce, "codex-a", approve=True)
    approved = approvals.decide(OWNER, request.nonce, "codex-a", approve=True)
    assert approved.payload == b"fixed\r"
    with pytest.raises(PermissionDenied):
        approvals.decide(OWNER, request.nonce, "codex-a", approve=True)


def test_expiration_rejection_and_disconnect_discard_input(security):
    _, approvals, _, clock = security
    r = approvals.request(OWNER, "codex-a", "input", b"\r")
    clock[0] = 60
    with pytest.raises(PermissionDenied, match="expired"):
        approvals.decide(OWNER, r.nonce, "codex-a", approve=True)
    r = approvals.request(OWNER, "codex-a", "input", b"\r")
    assert approvals.decide(OWNER, r.nonce, "codex-a", approve=False) is None
    r = approvals.request(OWNER, "codex-a", "input", b"\r")
    approvals.invalidate()
    with pytest.raises(PermissionDenied):
        approvals.decide(OWNER, r.nonce, "codex-a", approve=True)


def test_audit_is_metadata_only_owner_only_bounded_and_rejects_symlinks(security, tmp_path, monkeypatch):
    _, approvals, audit, _ = security
    monkeypatch.setenv("TEST_API_KEY", "extremely-private-value")
    r = approvals.request(OWNER, "codex-a", "input", b"API_KEY=extremely-private-value\r")
    approvals.decide(OWNER, r.nonce, "codex-a", approve=True)
    content = audit.path.read_text()
    assert "extremely-private-value" not in content
    assert r.nonce not in content
    assert "API_KEY" not in content
    assert stat.S_IMODE(audit.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit.path.parent.stat().st_mode) == 0o700
    assert all(set(json.loads(line)) == {"timestamp", "session_id", "user_id", "action", "decision", "source", "reason"} for line in content.splitlines())
    audit.max_bytes, audit.archives = 350, 2
    for _ in range(10):
        audit.record(session_id="codex-a", user_id=42, action="test", decision="denied", reason="test")
    assert len(list(audit.path.parent.glob("audit.jsonl.*.jsonl"))) <= 2
    with pytest.raises(AuditError):
        audit.record(session_id="codex-a", user_id=42, action="test", decision="denied", reason="API_KEY=secret")
    other = tmp_path / "other"
    other.write_text("preserved")
    audit.path.unlink()
    audit.path.symlink_to(other)
    with pytest.raises(AuditError):
        approvals.request(OWNER, "codex-a", "input", b"must not run")
    assert other.read_text() == "preserved"


def test_audit_failure_consumes_token_and_cannot_authorize_execution(security, monkeypatch):
    _, approvals, audit, _ = security
    r = approvals.request(OWNER, "codex-a", "input", b"\r")
    def fail(**kwargs):
        raise AuditError("disk_failed")
    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(AuditError):
        approvals.decide(OWNER, r.nonce, "codex-a", approve=True)
    assert r.nonce not in approvals.pending


def test_retention_discards_old_records_on_next_write_even_if_file_was_recently_modified(security):
    _, _, audit, _ = security
    audit.record(session_id="codex-a", user_id=42, action="test", decision="denied", reason="test")
    record = json.loads(audit.path.read_text())
    record["timestamp"] = "2000-01-01T00:00:00+00:00"
    audit.path.write_text(json.dumps(record) + "\n")
    audit.record(session_id="codex-a", user_id=42, action="test", decision="denied", reason="new_record")
    assert "2000-01-01" not in audit.path.read_text()
    assert not list(audit.path.parent.glob("audit.jsonl.*.jsonl"))

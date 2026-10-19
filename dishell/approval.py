"""Immutable, session-scoped, single-use confirmations. No PTY/Discord imports."""

import secrets
import time
from dataclasses import dataclass

from .permissions import Permission, PermissionDenied

OPERATIONS = {"input": Permission.INPUT, "start": Permission.MANAGE,
              "stop": Permission.MANAGE, "attach": Permission.MANAGE,
              "remote_off": Permission.MANAGE}


@dataclass(frozen=True)
class ApprovalRequest:
    nonce: str
    epoch: int
    session_id: str
    operation: str
    payload: bytes
    created_at: float
    expires_at: float
    screen_version: int | None = None
    input_revision: int | None = None

    @property
    def permission(self):
        return OPERATIONS[self.operation]


class ApprovalService:
    def __init__(self, policy, audit, *, ttl=60.0, clock=time.monotonic):
        if not 1 <= ttl <= 300:
            raise ValueError("approval TTL must be between 1 and 300 seconds")
        self.policy, self.audit = policy, audit
        self.ttl, self.clock = ttl, clock
        self.epoch = 0
        self.pending: dict[str, ApprovalRequest] = {}

    def _log(self, request, context, decision, reason):
        self.audit.record(session_id=request.session_id, user_id=context.user_id,
                          action=request.operation, decision=decision, reason=reason)

    def _release_reservation(self, request):
        if request.operation == "start":
            self.policy.unregister(request.session_id)

    def request(self, context, session_id, operation, payload=b"", *, screen_version=None, input_revision=None):
        self.policy.check(context, session_id, action="remote_attempt")
        if operation not in OPERATIONS or not isinstance(payload, bytes) or len(payload) > 65536:
            raise PermissionDenied("invalid_request")
        self.expire()
        if len(self.pending) >= 64:
            raise PermissionDenied("too_many_pending_approvals")
        now = self.clock()
        request = ApprovalRequest(secrets.token_urlsafe(24), self.epoch, session_id, operation,
                                  bytes(payload), now, now + self.ttl, screen_version, input_revision)
        self._log(request, context, "requested", "explicit_confirmation_required")
        self.pending[request.nonce] = request
        return request

    def decide(self, context, nonce, session_id, *, approve):
        self.policy.check(context, session_id, action="approval_callback")
        request = self.pending.get(nonce)
        if request is None or request.session_id != session_id or request.epoch != self.epoch:
            self.audit.record(session_id=session_id, user_id=context.user_id,
                              action="approval_callback", decision="denied", reason="invalid_nonce_or_scope")
            raise PermissionDenied("invalid_nonce_or_scope")
        # Consume before logging/execution. A log failure never leaves a usable token.
        del self.pending[nonce]
        if self.clock() >= request.expires_at:
            self._release_reservation(request)
            self._log(request, context, "expired", "approval_expired")
            raise PermissionDenied("approval_expired")
        if not approve:
            self._release_reservation(request)
        try:
            self._log(request, context, "approved" if approve else "rejected",
                      "explicit_confirmation" if approve else "owner_rejected")
        except BaseException:
            self._release_reservation(request)
            raise
        return request if approve else None

    def cancel(self, nonce):
        request = self.pending.pop(nonce, None)
        if request is not None:
            self._release_reservation(request)
            self.audit.record(session_id=request.session_id, user_id=self.policy.owner_id,
                              action=request.operation, decision="failed", reason="confirmation_delivery_failed")

    def expire(self):
        # Discard first, even if the disk is unavailable during expiration.
        expired = [r for r in self.pending.values() if self.clock() >= r.expires_at]
        for request in expired:
            self.pending.pop(request.nonce, None)
            self._release_reservation(request)
        for request in expired:
            self.audit.record(session_id=request.session_id, user_id=self.policy.owner_id,
                              action=request.operation, decision="expired", reason="approval_expired")

    def invalidate(self, session_id=None):
        discarded = [r for r in self.pending.values() if session_id is None or r.session_id == session_id]
        for request in discarded:
            self.pending.pop(request.nonce, None)
            self._release_reservation(request)
        if session_id is None:
            self.epoch += 1
        for request in discarded:
            self.audit.record(session_id=request.session_id, user_id=self.policy.owner_id,
                              action=request.operation, decision="invalidated", reason="connection_or_session_changed")

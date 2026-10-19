"""Fail-closed owner/DM/session checks. INPUT and MANAGE have no ambient grant."""

from dataclasses import dataclass
from enum import Enum


class Permission(str, Enum):
    VIEW = "VIEW"
    INPUT = "INPUT"
    MANAGE = "MANAGE"


class PermissionDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteContext:
    user_id: int
    is_dm: bool


class SecurityPolicy:
    def __init__(self, owner_id: int, audit):
        self.owner_id = owner_id
        self.audit = audit
        self.sessions: set[str] = set()

    def register(self, session_id):
        self.sessions.add(session_id)

    def unregister(self, session_id):
        self.sessions.discard(session_id)

    def check(self, context: RemoteContext, session_id: str = "global", *,
              permission=Permission.VIEW, confirmed=False, action="access"):
        reason = None
        if not self.owner_id or context.user_id != self.owner_id:
            reason = "not_owner"
        elif not context.is_dm:
            reason = "dm_required"
        elif session_id != "global" and session_id not in self.sessions:
            reason = "invalid_session"
        elif permission != Permission.VIEW and not confirmed:
            reason = "explicit_confirmation_required"
        if reason:
            # Never put untrusted session strings into metadata.
            safe_session = session_id if session_id in self.sessions else "global"
            self.audit.record(session_id=safe_session, user_id=max(0, context.user_id), action=action,
                              decision="denied", reason=reason)
            raise PermissionDenied(reason)

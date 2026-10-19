"""Owner-only, bounded JSONL metadata audit. No prompt/output parameters exist."""

import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class AuditError(RuntimeError):
    pass


class AuditLog:
    def __init__(self, path=None, *, max_bytes=1_048_576, retention_days=7, archives=5):
        state = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        self.path = Path(path or os.getenv("DISHELL_AUDIT_PATH", str(state / "dishell" / "audit.jsonl")))
        self.max_bytes = max_bytes
        self.retention_seconds = retention_days * 86400
        self.archives = archives
        self._lock = threading.Lock()

    def _prepare(self):
        parent = self.path.parent
        # Reject symlink traversal in all existing directory components.
        for part in (parent, *parent.parents):
            if part.is_symlink():
                raise AuditError("audit_directory_symlink")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AuditError("audit_directory_requires_owner_only_permissions")

    def _open(self):
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(fd)
            raise AuditError("audit_file_requires_owner_only_regular_file")
        return fd

    def _prune(self, now):
        candidates = sorted(self.path.parent.glob(self.path.name + ".*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(candidates):
            if path.is_symlink():
                raise AuditError("audit_archive_symlink")
            if index >= self.archives or now - path.stat().st_mtime > self.retention_seconds:
                path.unlink()

    def record(self, *, session_id, user_id, action, decision, source="discord", reason):
        # Only controlled identifiers are accepted; free-form diagnostics are rejected.
        for value in (session_id, action, decision, source, reason):
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", value):
                raise AuditError("audit_metadata_must_be_controlled_identifiers")
        if not isinstance(user_id, int) or user_id < 0:
            raise AuditError("invalid_audit_user")
        record = dict(timestamp=datetime.now(timezone.utc).isoformat(), session_id=session_id,
                      user_id=str(user_id), action=action, decision=decision, source=source, reason=reason)
        payload = (json.dumps(record, ensure_ascii=True) + "\n").encode()
        with self._lock:
            try:
                self._prepare()
                now = time.time()
                fd = self._open()
                try:
                    info = os.fstat(fd)
                    first_timestamp = now
                    if info.st_size:
                        first = os.pread(fd, 2048, 0).split(b"\n", 1)[0]
                        try:
                            first_timestamp = datetime.fromisoformat(json.loads(first)["timestamp"]).timestamp()
                        except (KeyError, TypeError, ValueError):
                            raise AuditError("invalid_existing_audit_metadata") from None
                    if info.st_size and (info.st_size + len(payload) > self.max_bytes or
                                         now - first_timestamp > min(86400, self.retention_seconds)):
                        os.close(fd)
                        fd = -1
                        archive = self.path.with_name(self.path.name + f".{time.time_ns()}.jsonl")
                        self.path.rename(archive)
                        os.utime(archive, (first_timestamp, first_timestamp), follow_symlinks=False)
                        fd = self._open()
                    view = memoryview(payload)
                    while view:
                        count = os.write(fd, view)
                        if not count:
                            raise OSError("short_audit_write")
                        view = view[count:]
                    os.fsync(fd)
                finally:
                    if fd >= 0:
                        os.close(fd)
                self._prune(now)
            except (OSError, ValueError) as exc:
                raise AuditError("audit_write_failed") from exc

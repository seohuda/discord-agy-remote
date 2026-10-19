"""Independent PTYs, screens, detectors and publishers on one asyncio loop."""

import asyncio
import codecs
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .diff import MAX_COLS, MAX_ROWS, BlockPublisher, ScreenDiffEngine
from .events import EventDetector, EventOutbox
from .launch import LaunchError
from .screen import TerminalScreen
from .terminal import PtyProcess


class Lifecycle(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class SessionError(RuntimeError):
    pass


@dataclass
class Session:
    id: str
    plugin: object
    argv: list[str]
    pty: object
    screen: TerminalScreen
    security_policy: object
    events: EventOutbox
    created_at: float = field(default_factory=time.time)
    state: Lifecycle = Lifecycle.CREATED
    publisher: BlockPublisher | None = None
    structured_mode: bool = False
    output_callback: object = None
    exit_callback: object = None

    def __post_init__(self):
        self.detector = EventDetector(self.id, self.plugin)
        self.diff = ScreenDiffEngine()
        self.history = deque(maxlen=10000)
        self.exit_code = None
        self.input_revision = 0
        self._loop = None
        self._writes = deque()
        self._writer_armed = False
        self._stop_lock = asyncio.Lock()
        self._exit_task = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._json_buffer = ""
        self._structured_events = deque()

    async def start(self):
        if self.state != Lifecycle.CREATED:
            raise SessionError("session_already_started")
        self.state = Lifecycle.STARTING
        self._loop = asyncio.get_running_loop()
        try:
            self.pty.spawn(self.screen.cols, self.screen.rows)
            self._loop.add_reader(self.pty.master_fd, self._readable)
            self.state = Lifecycle.RUNNING
        except BaseException:
            self.state = Lifecycle.FAILED
            await self.pty.shutdown()
            raise

    def _readable(self):
        try:
            data = self.pty.read()
            if data is None:
                self._loop.remove_reader(self.pty.master_fd)
                if self._exit_task is None:
                    self._exit_task = asyncio.create_task(self.stop(natural=True))
                return
            if not data:
                return
            self.screen.feed(data)
            self.history.extend(self.screen.pop_history())
            if self.structured_mode:
                self._json_buffer += self._decoder.decode(data)
                if len(self._json_buffer) > 131072:
                    self._json_buffer = ""  # oversized/invalid protocol line
                while "\n" in self._json_buffer:
                    line, self._json_buffer = self._json_buffer.split("\n", 1)
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and self.plugin.detect_structured(event).confidence:
                            self._structured_events.append(event)
                    except (ValueError, RecursionError):
                        pass
            if self.output_callback:
                self.output_callback(data)
        except OSError:
            if self._exit_task is None:
                self._exit_task = asyncio.create_task(self.stop())

    def observe(self):
        frame = self.screen.capture()
        snapshot = "\n".join(frame.lines)
        while self._structured_events:
            event = self.detector.update(snapshot, revision=frame.version,
                                         structured=self._structured_events.popleft())
            if event:
                self.events.put(event)
        event = self.detector.update(snapshot, revision=frame.version, exit_code=self.exit_code)
        if event:
            self.events.put(event)
        return frame

    def enqueue(self, data: bytes, *, acknowledged=False):
        if self.state != Lifecycle.RUNNING or self.pty.master_fd is None or self.pty.reap(block=False) is not None:
            raise SessionError("session_not_running")
        if sum(len(item[0]) for item in self._writes) + len(data) > 1_048_576:
            raise SessionError("input_queue_full")
        future = self._loop.create_future() if acknowledged else None
        if data:
            self.input_revision += 1
            self._writes.append([bytes(data), future])
            self._drain()
        elif future:
            future.set_result(None)
        return future

    async def write(self, data, *, timeout=None):
        future = self.enqueue(data, acknowledged=True)
        try:
            if timeout is None:
                await future
            else:
                await asyncio.wait_for(future, timeout)
        finally:
            self._writes = deque(item for item in self._writes if item[1] is not future)
            self._disarm_empty_writer()

    def cancel_remote_writes(self):
        for _, future in self._writes:
            if future is not None and not future.done():
                future.set_exception(SessionError("remote_write_revoked"))
        self._writes = deque(item for item in self._writes if item[1] is None)
        self._disarm_empty_writer()

    def _disarm_empty_writer(self):
        if not self._writes and self._writer_armed and self.pty.master_fd is not None:
            self._loop.remove_writer(self.pty.master_fd)
            self._writer_armed = False

    def _drain(self):
        try:
            while self._writes and self.state == Lifecycle.RUNNING:
                item = self._writes[0]
                item[0] = self.pty.write(item[0], strict=True)
                if item[0]:
                    if not self._writer_armed:
                        self._loop.add_writer(self.pty.master_fd, self._drain)
                        self._writer_armed = True
                    return
                self._writes.popleft()
                if item[1] is not None and not item[1].done():
                    item[1].set_result(None)
        except OSError:
            self._cancel_writes()
        if self._writer_armed and self.pty.master_fd is not None:
            self._loop.remove_writer(self.pty.master_fd)
            self._writer_armed = False

    def _cancel_writes(self):
        while self._writes:
            _, future = self._writes.popleft()
            if future is not None and not future.done():
                future.set_exception(SessionError("pty_write_failed"))

    def resize(self, cols, rows, *, remote=False):
        if remote:
            cols, rows = min(cols, MAX_COLS), min(rows, MAX_ROWS)
        cols, rows = max(cols, 1), max(rows, 1)
        self.pty.set_winsize(cols, rows)
        self.screen.resize(cols, rows)

    async def stop(self, *, natural=False):
        async with self._stop_lock:
            if self.state in (Lifecycle.STOPPED, Lifecycle.FAILED):
                return
            cleanup = asyncio.create_task(self._stop(natural=natural))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _stop(self, *, natural):
        was_created = self.state == Lifecycle.CREATED
        self.state = Lifecycle.STOPPING
        try:
            if self._loop and self.pty.master_fd is not None:
                self._loop.remove_reader(self.pty.master_fd)
                self._loop.remove_writer(self.pty.master_fd)
            self._cancel_writes()
            if natural:
                # EOF can precede waitpid visibility by a scheduling quantum.
                for _ in range(10):
                    if self.pty.reap(block=False) is not None:
                        break
                    await asyncio.sleep(.01)
            code = await self.pty.shutdown()
            self.exit_code = None if was_created else code
            self.state = Lifecycle.STOPPED if self.exit_code in (None, 0, 130, 143) else Lifecycle.FAILED
        finally:
            self.pty.close()
            self._cancel_writes()
        self.observe()
        if self.exit_callback:
            self.exit_callback(self)


class SessionManager:
    def __init__(self, plugins, policy, audit, *, max_sessions=3, process_factory=PtyProcess):
        if not 1 <= max_sessions <= 16:
            raise ValueError("max_sessions must be between 1 and 16")
        self.plugins, self.policy, self.audit = plugins, policy, audit
        self.max_sessions = max_sessions
        self.process_factory = process_factory
        self.sessions: dict[str, Session] = {}
        self.active_id: str | None = None
        self.events = EventOutbox()
        self._lock = asyncio.Lock()
        self.closed = False

    def new_id(self, plugin_id):
        if plugin_id not in self.plugins:
            raise LaunchError("지원 CLI: agy, codex, claude, opencode")
        return f"{plugin_id}-{uuid.uuid4().hex[:10]}"

    def get(self, session_id):
        try:
            return self.sessions[session_id]
        except KeyError:
            raise SessionError("invalid_session") from None

    def create(self, plugin_id, *, argv=None, args=None, cols=60, rows=30, session_id=None):
        if self.closed:
            raise SessionError("session_manager_closed")
        if sum(s.state in (Lifecycle.CREATED, Lifecycle.STARTING, Lifecycle.RUNNING, Lifecycle.STOPPING)
               for s in self.sessions.values()) >= self.max_sessions:
            raise SessionError("maximum_sessions_reached")
        sid = session_id or self.new_id(plugin_id)
        if sid in self.sessions:
            raise SessionError("duplicate_session_id")
        plugin = self.plugins.get(plugin_id)
        if plugin is None:
            raise LaunchError("unsupported_plugin")
        args = args or []
        argv = list(argv) if argv is not None else plugin.build_argv(args)
        session = Session(sid, plugin, argv, self.process_factory(argv),
                          TerminalScreen(cols, rows), self.policy, self.events,
                          structured_mode=plugin.uses_jsonl(args))
        self.sessions[sid] = session
        self.policy.register(sid)
        return session

    async def launch(self, session, *, source="discord"):
        async with self._lock:
            if self.closed:
                raise SessionError("session_manager_closed")
            try:
                self.audit.record(session_id=session.id, user_id=self.policy.owner_id, action="session_start",
                                  decision="starting", source=source, reason="allowlisted_cli")
                await session.start()
                self.audit.record(session_id=session.id, user_id=self.policy.owner_id, action="session_start",
                                  decision="started", source=source, reason="pty_spawned")
            except BaseException:
                await session.stop()
                raise
            if self.active_id is None:
                self.active_id = session.id
        return session

    def attach(self, session_id):
        session = self.get(session_id)
        if session.state != Lifecycle.RUNNING:
            raise SessionError("session_not_running")
        self.active_id = session_id
        return session

    async def stop(self, session_id, *, source="discord"):
        session = self.get(session_id)
        self.audit.record(session_id=session_id, user_id=self.policy.owner_id, action="session_stop",
                          decision="stopping", source=source, reason="explicit_stop")
        await session.stop()
        if self.active_id == session_id:
            self.active_id = None
        self.audit.record(session_id=session_id, user_id=self.policy.owner_id, action="session_stop",
                          decision="stopped", source=source, reason="pty_closed")

    async def close(self):
        self.closed = True
        # Cleanup is unconditional even when the audit disk is unavailable.
        results = await asyncio.gather(*(s.stop() for s in self.sessions.values()), return_exceptions=True)
        self.active_id = None
        for result in results:
            if isinstance(result, BaseException):
                raise result
        for session in self.sessions.values():
            self.audit.record(session_id=session.id, user_id=self.policy.owner_id, action="session_stop",
                              decision="stopped", source="local", reason="manager_shutdown")

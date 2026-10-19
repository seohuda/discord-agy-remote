"""Local TUI wiring and one Discord connection over isolated CLI sessions."""

import asyncio
import contextlib
import os
import signal
import time
from datetime import datetime, timezone

import discord

from .alerts import APPROVAL, DONE
from .approval import ApprovalService
from .audit import AuditError, AuditLog
from .bot import DishellBot
from .commands import parse_remote_message
from .diff import BlockPublisher, utf16_len
from .localcmd import LocalCommandFilter
from .permissions import PermissionDenied, RemoteContext, SecurityPolicy
from .plugins import default_plugins
from .redaction import redact
from .screen import render_for_discord
from .sessions import Lifecycle, SessionError, SessionManager
from .terminal import LocalTerminal, effective_size

PUBLISH_INTERVAL = 1.5
DEFAULT_REMOTE_COLS = 60
READY_TIMEOUT = 20.0
ALERT_HEADERS = {
    APPROVAL: "⏸️ **입력 대기 중** — agy가 승인을 기다리고 있습니다.",
    DONE: "✅ **작업 완료** — 명시적 완료 신호를 확인했습니다.",
}
FORBIDDEN_HELP = (
    "[Dishell] 디스코드가 DM 전송을 거부했습니다(403).\n"
    "  · 봇과 같은 서버에 참여하고 DM 허용 설정을 확인하세요.\n"
    "  · DISCORD_USER_ID 가 본인 ID인지 확인하세요."
)


def _int_env(name, default):
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else default


class _DiscordTransport:
    def __init__(self, manager, session):
        self.manager, self.session = manager, session
        self.header = None

    def _embed(self, index, text):
        return discord.Embed(title=f"{self.session.id} · {self.session.plugin.display_name} · 블록 {index + 1}",
                             description=text, color=discord.Color.blurple())

    async def send(self, index, text):
        user = await self.manager._get_user()
        if user is None:
            raise ConnectionError("discord_unavailable")
        return await user.send(content=self.header if index == 0 else None, embed=self._embed(index, text))

    async def edit(self, message, index, text):
        await message.edit(embed=self._embed(index, text))

    async def delete(self, message):
        await message.delete()


class DishellManager:
    def __init__(self, token: str, user_id: int, argv: list[str],
                 interval: float = PUBLISH_INTERVAL, *, plugin_id="agy", plugins=None,
                 local_args=None, audit=None, max_sessions=None, approval_ttl=None,
                 auto_remote=False):
        self.token, self.user_id, self.argv = token, user_id, argv
        self.interval = max(.05, interval)
        self.auto_remote = auto_remote
        self.local = LocalTerminal()
        self.audit = audit or AuditLog()
        self.policy = SecurityPolicy(user_id, self.audit)
        self.approvals = ApprovalService(self.policy, self.audit,
            ttl=approval_ttl if approval_ttl is not None else _int_env("DISHELL_APPROVAL_TTL", 60))
        self.sessions = SessionManager(plugins or default_plugins(), self.policy, self.audit,
            max_sessions=max_sessions if max_sessions is not None else _int_env("DISHELL_MAX_SESSIONS", 3))
        cols, rows = self.local.size()
        self.primary = self.sessions.create(plugin_id, argv=argv, args=local_args, cols=cols, rows=rows)
        self.primary.output_callback = self._local_output
        self.primary.exit_callback = self._primary_exited
        # Existing embedding callers can still inspect the local PTY and screen.
        self.pty, self.screen = self.primary.pty, self.primary.screen
        self.filter = LocalCommandFilter()
        self.rc_enabled = False
        self._online = False
        self._starting = False
        self.client = None
        self.bot_task = self.publish_task = None
        self._loop = self._finished = self._user = None
        self._status_shown = False
        self._sticky_text = ""
        self._debug_path = os.getenv("DISHELL_DEBUG", "")
        self.remote_cols = _int_env("DISHELL_COLS", DEFAULT_REMOTE_COLS)
        self._event_retry_at = 0.0
        self._event_failures = 0
        self._event_publish_lock = asyncio.Lock()
        self._tasks = set()
        self._last_snapshot = None
        self._live_messages = []
        self._start_new_message = False
        self._pending_alert = None
        self._legacy_revision = 0

    def _debug(self, message):
        # Diagnostics contain identifiers/counts, never input/output or argv.
        if self._debug_path:
            with contextlib.suppress(OSError):
                fd = os.open(self._debug_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "a") as handle:
                    handle.write(f"{time.time():.3f} {redact(message)}\n")

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._finished = self._loop.create_future()
        try:
            await self.sessions.launch(self.primary, source="local")
            self.local.enter_raw()
            if self.local.is_tty:
                self._loop.add_reader(self.local.fd, self._on_stdin_readable)
            self.local.on_resize(self._loop, self._on_resize)
            self._loop.add_signal_handler(signal.SIGTERM, self._finish)
            if self.auto_remote:
                await self._start_bot()
            await self._finished
        finally:
            await self._shutdown()
        return self.primary.exit_code or 0

    async def _shutdown(self):
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._loop and self.local.is_tty:
            self._loop.remove_reader(self.local.fd)
        if self._loop:
            with contextlib.suppress(Exception):
                self._loop.remove_signal_handler(signal.SIGWINCH)
                self._loop.remove_signal_handler(signal.SIGTERM)
        try:
            await self.sessions.close()
            if self.rc_enabled and self._online:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._publish_events(), timeout=3)
            if self.rc_enabled and len(self.sessions.events):
                self.local.notice(f"[Dishell] 종료 시 미전송 알림 {len(self.sessions.events)}개 · 감사 로그에 실패 기록")
                self.audit.record(session_id="global", user_id=self.user_id, action="notification",
                                  decision="failed", source="local", reason="shutdown_with_pending_notifications")
        finally:
            try:
                await self._stop_bot(announce="🔴 **원격 제어 종료** — 로컬 CLI 세션이 끝났습니다.")
            finally:
                self.local.restore()

    def _finish(self):
        if self._finished is not None and not self._finished.done():
            self._finished.set_result(None)

    def _primary_exited(self, session):
        self._session_exited(session)
        self._finish()

    def _background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled() and done.exception():
                self._debug(f"background_failed={type(done.exception()).__name__}")
        task.add_done_callback(finished)
        return task

    def _local_output(self, data):
        self.local.write(data)
        if self._sticky_text:
            self.local.status(self._sticky_text)

    def _on_pty_readable(self):
        self.primary._readable()

    def _on_stdin_readable(self):
        try:
            data = os.read(self.local.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._finish()
            return
        if not data:
            self._finish()
            return
        self._clear_sticky()
        forward, commands = self.filter.feed(data)
        self._debug(f"local_input_bytes={len(data)} forwarded_bytes={len(forward)}")
        self._write_to_agy(forward)
        self._show_pending(self.filter.pending)
        for command in commands:
            self._background(self._handle_local_command(command))

    def _write_to_agy(self, data):
        if data:
            try:
                self.primary.enqueue(data)
            except SessionError as exc:
                self.local.notice(f"[Dishell] 로컬 입력 실패: {exc}")

    def _sticky(self, text):
        self._sticky_text = text
        self.local.status(text)
        self._status_shown = True

    def _clear_sticky(self):
        if self._sticky_text:
            self._sticky_text = ""
            self.local.status()
            self._status_shown = False

    def _show_pending(self, pending):
        if pending:
            self.local.status(f"[Dishell] {pending.decode('utf-8', errors='replace')}")
            self._status_shown = True
        elif self._status_shown:
            self.local.status()
            self._status_shown = False

    def _size(self):
        return effective_size(self.local.size(), self.remote_cols, self.rc_enabled)

    def _apply_size(self):
        self.primary.resize(*self._size(), remote=self.rc_enabled)

    def _on_resize(self):
        self._apply_size()

    async def _force_repaint(self):
        cols, rows = self.screen.cols, self.screen.rows
        self.pty.set_winsize(cols, max(rows - 1, 1))
        await asyncio.sleep(.05)
        self.pty.set_winsize(cols, rows)

    async def _handle_local_command(self, command):
        if command == "/rc on":
            if self.rc_enabled:
                self.local.notice("[Dishell] 원격 제어는 이미 켜져 있습니다.")
            else:
                await self._start_bot()
        elif command == "/rc off":
            await self._stop_bot(announce="🔴 **원격 제어 종료**")
            self.local.notice("[Dishell] 원격 제어를 껐습니다.")
        elif command == "/rc status":
            self.local.notice(f"[Dishell] 원격 제어: {'ON' if self.rc_enabled else 'OFF'} · {self.primary.id}")
        await self._force_repaint()

    async def _start_bot(self):
        if self.rc_enabled or self._starting:
            return
        if not self.token or not self.user_id:
            self.local.notice("[Dishell] .env 의 DISCORD_TOKEN / DISCORD_USER_ID 가 설정되지 않았습니다.")
            return
        self._starting = True
        ready = None
        try:
            self.client = DishellBot(manager=self)
            self.bot_task = asyncio.create_task(self.client.start(self.token))
            ready = asyncio.create_task(self.client.wait_until_ready())
            done, _ = await asyncio.wait({ready, self.bot_task}, timeout=READY_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if ready not in done or self.bot_task in done:
                reason = "시간 초과 (네트워크 또는 토큰 확인)"
                if self.bot_task in done:
                    exc = self.bot_task.exception()
                    reason = "토큰/Intent/네트워크 오류: " + type(exc).__name__
                self._sticky(f"[Dishell] 디스코드 연결 실패 · {reason}")
                await self._stop_bot()
                return
            if not await self._notify("🟢 **원격 제어 시작** — 기본 읽기 전용입니다. 입력·관리는 버튼 승인이 필요합니다. `/help`"):
                self._sticky("[Dishell] DM 을 보낼 수 없습니다 · 토큰/DM 설정 확인")
                await self._stop_bot()
                return
            self.rc_enabled = self._online = True
            self._apply_size()
            self.publish_task = asyncio.create_task(self._publish_loop())
            self.local.notice("[Dishell] 디스코드 DM 연결 · 입력은 명시적 승인 필요")
        finally:
            self._starting = False
            if ready and not ready.done():
                ready.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ready

    def _invalidate_remote(self):
        try:
            self.approvals.invalidate()
        except AuditError:
            self.local.notice("[Dishell] 감사 로그 실패 · 승인 요청을 모두 폐기했습니다.")
        for session in self.sessions.sessions.values():
            session.cancel_remote_writes()
            if session.publisher:
                session.publisher.reconnect()

    async def _stop_bot(self, announce=None):
        self._online = False
        self._invalidate_remote()
        if announce and self.rc_enabled:
            await self._notify(announce)
        was_enabled, self.rc_enabled = self.rc_enabled, False
        if was_enabled:
            self._apply_size()
        if self.publish_task and self.publish_task is not asyncio.current_task():
            self.publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.publish_task
        self.publish_task = None
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.close()
        self.client = None
        if self.bot_task:
            self.bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.bot_task
        self.bot_task = None
        self._user = None
        self._live_messages = []

    def on_bot_ready(self, username):
        self._invalidate_remote()
        self._online = True
        self.local.notice(f"[Dishell] 디스코드 로그인: {username}")

    def on_bot_disconnect(self):
        self._online = False
        self._invalidate_remote()

    async def _get_user(self):
        if self._user is not None:
            return self._user
        if not self.client or not self.user_id:
            return None
        self._user = self.client.get_user(self.user_id)
        if self._user is None:
            try:
                self._user = await self.client.fetch_user(self.user_id)
            except Exception as exc:
                self.local.notice(f"[Dishell] 사용자 조회 실패: {type(exc).__name__}")
        return self._user

    async def _notify(self, text, color=None):
        user = await self._get_user()
        if user is None:
            return False
        try:
            await user.send(embed=discord.Embed(description=redact(text)[:3900], color=color or discord.Color.green()))
            return True
        except discord.Forbidden:
            self.local.notice(FORBIDDEN_HELP)
        except Exception as exc:
            self._debug(f"notification_failed={type(exc).__name__}")
        return False

    def _check_remote(self, context, session_id="global", action="access"):
        self.policy.check(context, session_id, action=action)
        if not self.rc_enabled or not self._online:
            self.audit.record(session_id="global", user_id=context.user_id, action=action,
                              decision="denied", reason="remote_disconnected")
            raise PermissionDenied("remote_disconnected")

    def status_text(self, session_id=None):
        sid = session_id or self.sessions.active_id
        if not sid:
            return "활성 세션이 없습니다. `/sessions` · `/session attach <id>`"
        s = self.sessions.get(sid)
        metrics = s.publisher.metrics if s.publisher else None
        details = ""
        if metrics:
            details = (f"\nsnapshots={s.screen.snapshot_count}, edits={metrics.discord_edits}, "
                       f"rows={metrics.changed_rows}, blocks={metrics.changed_blocks}, "
                       f"duplicates={metrics.duplicate_updates}, full={metrics.full_refreshes}, "
                       f"queue_wait={metrics.queue_wait_seconds:.3f}s")
        return (f"[{s.id}] {s.plugin.display_name} · {s.state.value} · {s.detector.state.value}"
                f"\npid {s.pty.pid} · 화면 {s.screen.cols}x{s.screen.rows} · exit={s.exit_code}"
                f"\n원격 권한: VIEW · INPUT/MANAGE: 매 작업 일회성 승인{details}")

    def remote_off(self):
        # Internal teardown only; all remote entry points require confirmation.
        self._background(self._stop_bot())
        self._background(self._force_repaint())

    def send_input(self, payload):
        raise PermissionDenied("explicit_confirmation_required")

    async def handle_remote_message(self, message):
        context = RemoteContext(message.author.id, isinstance(message.channel, discord.DMChannel))
        async def respond(text, view=None):
            kwargs = {"mention_author": False, "allowed_mentions": discord.AllowedMentions.none()}
            if view is not None:
                kwargs["view"] = view
            return await message.reply(text, **kwargs)
        await self.handle_action(context, parse_remote_message(message.content), respond)

    async def handle_action(self, context, action, respond):
        request = None
        try:
            is_input = action.kind in ("text", "keys")
            self._check_remote(context, action="remote_input_attempt" if is_input else "remote_command")
            if is_input:
                sid = self.sessions.active_id
                self.audit.record(session_id=sid if sid in self.policy.sessions else "global",
                                  user_id=context.user_id, action="remote_input", decision="attempted",
                                  reason="input_received")
            if action.kind in ("help", "error"):
                await respond(action.message)
                return
            if action.kind == "sessions":
                text = "\n".join(f"{'*' if s.id == self.sessions.active_id else '-'} {s.id} · {s.plugin.display_name} · {s.state.value}"
                                 for s in self.sessions.sessions.values()) or "세션이 없습니다."
                await respond(text[:1900])
                return
            if action.kind == "rc_status":
                await respond(self.status_text())
                return
            if action.kind in ("session_status", "session_history"):
                sid = action.arguments[0]
                self._check_remote(context, sid)
                session = self.sessions.get(sid)
                if action.kind == "session_status":
                    await respond(self.status_text(sid))
                else:
                    page = int(action.arguments[1]) if len(action.arguments) > 1 else 0
                    lines = list(session.history)
                    end = max(0, len(lines) - page * 100)
                    lines = lines[max(0, end - 100):end]
                    for chunk in render_for_discord(redact("\n".join(lines)) or "(기록 없음)", limit=1800):
                        await respond(chunk)
                return
            if action.kind == "session_start":
                plugin_id = action.arguments[0]
                # Resolve only a built-in, locally configured executable. No remote argv.
                self.sessions.plugins[plugin_id].build_argv([])
                sid = self.sessions.new_id(plugin_id)
                self.policy.register(sid)
                try:
                    request = self.approvals.request(context, sid, "start", plugin_id.encode())
                except BaseException:
                    self.policy.unregister(sid)
                    raise
            elif action.kind in ("session_stop", "session_attach"):
                sid = action.arguments[0]
                self._check_remote(context, sid)
                self.sessions.get(sid)
                request = self.approvals.request(context, sid, action.kind.removeprefix("session_"))
            elif action.kind == "rc_off":
                request = self.approvals.request(context, "global", "remote_off")
            elif action.kind in ("text", "keys"):
                sid = self.sessions.active_id
                if sid is None:
                    raise PermissionDenied("no_active_session")
                self._check_remote(context, sid, action="remote_input_attempt")
                session = self.sessions.get(sid)
                if session.state != Lifecycle.RUNNING:
                    raise PermissionDenied("session_not_running")
                request = self.approvals.request(context, sid, "input", action.payload,
                                                 screen_version=session.screen.version,
                                                 input_revision=session.input_revision)
            else:
                raise PermissionDenied("unsupported_action")
            from .bot import ApprovalView
            view = ApprovalView(self, request)
            # repr exposes CR/ESC/control bytes; preview cannot hide an Enter.
            preview = discord.utils.escape_markdown(redact(repr(request.payload)))
            if utf16_len(preview) > 1200:
                self.approvals.decide(context, request.nonce, request.session_id, approve=False)
                raise PermissionDenied("input_too_long_for_confirmation")
            text = (f"[{request.session_id}] {request.permission.value} · {request.operation}\n"
                    f"실행할 고정 입력: {preview}\n"
                    f"{self.approvals.ttl:g}초 이내 승인/거부. 화면이 바뀌면 입력 승인은 폐기됩니다.\n"
                    "CLI 자체 승인 화면의 선택도 직접 검토하세요. 자동 Enter는 전송하지 않습니다.")
            view.message = await respond(text, view=view)
        except asyncio.CancelledError:
            # A cancelled HTTP await may already have delivered the button.
            # Revoke its capability before propagating cancellation; Exception
            # does not catch CancelledError on supported Python versions.
            if request is not None:
                try:
                    self.approvals.cancel(request.nonce)
                except AuditError:
                    self._online = False
                    self._invalidate_remote()
                    self.local.notice("[Dishell] 승인 취소 감사 로그 실패 · 원격 작업 차단")
            raise
        except Exception as exc:
            error = exc
            if request is not None:
                try:
                    self.approvals.cancel(request.nonce)
                except AuditError as audit_exc:
                    error = audit_exc
                if request.operation == "start":
                    self.policy.unregister(request.session_id)
            # Exception text can contain executable arguments or environment secrets.
            code = str(error) if isinstance(error, (PermissionDenied, SessionError, AuditError)) else type(error).__name__
            from .launch import LaunchError
            if isinstance(error, LaunchError):
                code = redact(str(error))
            await respond(f"작업을 거부했습니다: {code}")

    async def resolve_approval(self, context, nonce, session_id, *, approve):
        self._check_remote(context, session_id, action="approval_callback")
        request = self.approvals.decide(context, nonce, session_id, approve=approve)
        if request is None:
            return "거부했습니다. 입력을 폐기했습니다."
        # No await between token consumption, revalidation and starting the write.
        self.policy.check(context, session_id, permission=request.permission, confirmed=True,
                          action="approved_operation")
        try:
            if request.operation == "input":
                session = self.sessions.get(session_id)
                if (session.screen.version != request.screen_version or
                        session.input_revision != request.input_revision):
                    raise PermissionDenied("screen_changed_request_again")
                # Expired/disconnected buffered input is discarded, including PTY
                # backpressure. Bytes already accepted by the child cannot be undone.
                await session.write(request.payload, timeout=max(.001, request.expires_at - self.approvals.clock()))
            elif request.operation == "start":
                session = self.sessions.create(request.payload.decode(), session_id=session_id)
                session.exit_callback = self._session_exited
                await self.sessions.launch(session)
            elif request.operation == "stop":
                self.approvals.invalidate(session_id)
                await self.sessions.stop(session_id)
            elif request.operation == "attach":
                self._invalidate_remote()
                session = self.sessions.attach(session_id)
                if session.publisher:
                    session.publisher.reconnect()
            elif request.operation == "remote_off":
                self.remote_off()
            self.audit.record(session_id=session_id, user_id=context.user_id, action=request.operation,
                              decision="executed", reason="approved_operation_finished")
            return f"[{session_id}] {request.operation} 실행 완료"
        except BaseException:
            try:
                self.audit.record(session_id=session_id, user_id=context.user_id, action=request.operation,
                                  decision="failed", reason="execution_or_state_check_failed")
            except AuditError:
                # A completed PTY write cannot be undone. Revoke remote access.
                self._online = False
                self._invalidate_remote()
                self.local.notice("[Dishell] 감사 로그 실패 · 추가 원격 작업을 차단했습니다.")
            raise

    def _session_exited(self, session):
        if self.sessions.active_id == session.id:
            self.sessions.active_id = None
        try:
            self.approvals.invalidate(session.id)
            self.audit.record(session_id=session.id, user_id=self.user_id, action="session_exit",
                              decision="stopped", source="process", reason="process_exited")
        except AuditError:
            self._online = False
            self.local.notice("[Dishell] 세션 종료 감사 로그 실패 · 원격 쓰기 차단")

    def _publisher(self, session):
        if session.publisher is None:
            session.publisher = BlockPublisher(_DiscordTransport(self, session))
        return session.publisher

    async def _publish_loop(self):
        while True:
            await asyncio.sleep(self.interval)  # batch output; no per-byte Discord work
            if not self.rc_enabled or not self._online:
                continue
            try:
                self.approvals.expire()
                for session in list(self.sessions.sessions.values()):
                    frame = session.observe()
                    if session.id == self.sessions.active_id:
                        publisher = self._publisher(session)
                        if getattr(session, "_published_frame", None) is not frame or publisher._force:
                            publisher.submit(session.diff.update(frame))
                            session._published_frame = frame
                        else:
                            publisher.metrics.duplicate_updates += 1
                        publisher.metrics.snapshots = session.screen.snapshot_count
                        await publisher.flush()
                await self._publish_events()
            except asyncio.CancelledError:
                raise
            except AuditError:
                self._online = False
                self._invalidate_remote()
                self.local.notice("[Dishell] 감사 로그 오류 · 원격 작업 차단")
            except Exception as exc:
                self._debug(f"publish_failed={type(exc).__name__}")

    async def _publish_events(self):
        # Shutdown and the regular publisher can reach this path together.
        # Keep peek/send/acknowledge in one consumer transaction.
        async with self._event_publish_lock:
            if time.monotonic() < self._event_retry_at:
                return
            for _ in range(4):
                event = self.sessions.events.peek()
                if event is None:
                    return
                stamp = datetime.fromtimestamp(event.timestamp, timezone.utc).strftime("%H:%M:%S UTC")
                text = (f"[Session: {event.session_id}]\n상태: **{event.event_type.value}**\n"
                        f"도구: {self.sessions.plugins[event.plugin_id].display_name}\n"
                        f"메시지: {redact(event.reason)}\n시간: {stamp} · event #{event.sequence}\n현재 세션을 확인하세요.")
                if not await self._notify(text):
                    self._event_failures += 1
                    self._event_retry_at = time.monotonic() + min(60, 2 ** min(self._event_failures, 5))
                    return
                self.sessions.events.acknowledge(event)
                self._event_failures = 0
                self._event_retry_at = 0

    async def _publish(self, snapshot, history=None):
        """Compatibility helper for callers publishing pre-rendered text snapshots.

        The running engine uses fixed-row ScreenFrames; old callers retain their
        bounded tail/history rendering with per-block duplicate suppression.
        """
        if await self._get_user() is None:
            return
        publisher = self._publisher(self.primary)
        if self._start_new_message:
            publisher.reconnect()
        publisher.transport.header = ALERT_HEADERS.get(self._pending_alert or "")
        self._legacy_revision += 1
        publisher.submit_blocks(self._legacy_revision, render_for_discord(snapshot, history))
        if await publisher.flush():
            self._start_new_message = False
            self._pending_alert = None
            self._last_snapshot = snapshot
        self._live_messages = publisher.messages

"""Wiring between the local agy PTY session and the Discord bridge."""

import asyncio
import contextlib
import os
import time

import discord

from .alerts import APPROVAL, DONE, AlertDetector
from .bot import DishellBot
from .commands import parse_remote_message
from .localcmd import LocalCommandFilter
from .screen import TerminalScreen, render_for_discord
from .terminal import LocalTerminal, PtyProcess, effective_size

PUBLISH_INTERVAL = 1.5
# Discord's code block folds past roughly this width.
DEFAULT_REMOTE_COLS = 60
READY_TIMEOUT = 20.0

ALERT_HEADERS = {
    APPROVAL: "⏸️ **입력 대기 중** — agy가 승인을 기다리고 있습니다.",
    DONE: "✅ **작업 완료** — agy가 유휴 상태입니다.",
}

FORBIDDEN_HELP = (
    "[Dishell] 디스코드가 DM 전송을 거부했습니다(403).\n"
    "  · 봇과 내가 같은 서버에 하나 이상 함께 있어야 DM을 보낼 수 있습니다.\n"
    "  · 서버의 개인정보 설정에서 '서버 멤버가 보내는 DM 허용'을 켜주세요.\n"
    "  · DISCORD_USER_ID 가 본인 ID가 맞는지 확인하세요."
)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else default


class DishellManager:
    def __init__(self, token: str, user_id: int, argv: list[str],
                 interval: float = PUBLISH_INTERVAL):
        self.token = token
        self.user_id = user_id
        self.argv = argv
        self.interval = interval

        self.local = LocalTerminal()
        self.pty = PtyProcess(argv)
        self.screen = TerminalScreen(*self.local.size())
        self.filter = LocalCommandFilter()
        self.detector = AlertDetector()

        self.rc_enabled = False
        self.client: DishellBot | None = None
        self.bot_task: asyncio.Task | None = None
        self.publish_task: asyncio.Task | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._finished: asyncio.Future | None = None
        self._user: discord.User | None = None
        self._live_messages: list[discord.Message] = []
        self._last_snapshot: str | None = None
        self._start_new_message = False
        self._pending_alert: str | None = None
        self._backlog = bytearray()
        self._writer_armed = False
        self._debug_path = os.getenv("DISHELL_DEBUG", "")
        self._status_shown = False
        self._sticky_text = ""
        self.remote_cols = _int_env("DISHELL_COLS", DEFAULT_REMOTE_COLS)

    def _debug(self, message: str) -> None:
        """Append a diagnostic line when DISHELL_DEBUG points at a file."""
        if not self._debug_path:
            return
        with contextlib.suppress(OSError):
            with open(self._debug_path, "a", encoding="utf-8") as handle:
                handle.write(f"{time.time():.3f} {message}\n")

    # ------------------------------------------------------------------ run

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._finished = self._loop.create_future()

        cols, rows = self.local.size()
        self._debug(
            f"start argv={self.argv} size={cols}x{rows} "
            f"stdin_fd={self.local.fd} is_tty={self.local.is_tty} pid={os.getpid()}"
        )
        self.pty.spawn(cols, rows)
        self.local.enter_raw()

        self._loop.add_reader(self.pty.master_fd, self._on_pty_readable)
        if self.local.is_tty:
            self._loop.add_reader(self.local.fd, self._on_stdin_readable)
        self.local.on_resize(self._loop, self._on_resize)

        try:
            await self._finished
        finally:
            await self._shutdown()
        return self.pty.reap() or 0

    async def _shutdown(self) -> None:
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self.pty.master_fd)
            with contextlib.suppress(Exception):
                self._loop.remove_writer(self.pty.master_fd)
            if self.local.is_tty:
                with contextlib.suppress(Exception):
                    self._loop.remove_reader(self.local.fd)
        await self._stop_bot(announce="🔴 **원격 제어 종료** — agy 세션이 끝났습니다.")
        self.pty.terminate()
        self.pty.reap()
        self.pty.close()
        self.local.restore()

    def _finish(self) -> None:
        if self._finished is not None and not self._finished.done():
            self._finished.set_result(None)

    # --------------------------------------------------------------- local

    def _on_pty_readable(self) -> None:
        data = self.pty.read()
        if data is None:
            self._finish()
            return
        if not data:
            return
        self.local.write(data)
        if self._sticky_text:
            self.local.status(self._sticky_text)  # agy just repainted over it
        if self.rc_enabled:
            self.screen.feed(data)

    def _on_stdin_readable(self) -> None:
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
        self._debug(
            f"stdin={data!r} -> agy={forward!r} held={self.filter.pending!r} cmds={commands}"
        )
        self._write_to_agy(forward)
        self._show_pending(self.filter.pending)
        for command in commands:
            asyncio.create_task(self._handle_local_command(command))

    def _sticky(self, text: str) -> None:
        """Show a message that survives agy's repaints.

        Plain notices are wiped the moment agy redraws, which is exactly what
        happens right after a failed ``/rc on`` -- so failures were invisible.
        """
        self._sticky_text = text
        self.local.status(text)
        self._status_shown = True

    def _clear_sticky(self) -> None:
        if self._sticky_text:
            self._sticky_text = ""
            self.local.status()
            self._status_shown = False

    def _show_pending(self, pending: bytes) -> None:
        """Echo a local command while it is being withheld from agy."""
        if pending:
            text = pending.decode("utf-8", errors="replace")
            self.local.status(f"[Dishell] {text}")
            self._status_shown = True
        elif self._status_shown:
            self.local.status()
            self._status_shown = False

    def _write_to_agy(self, data: bytes) -> None:
        """Queue ``data`` for agy, retrying whatever the PTY could not take."""
        if data:
            self._backlog += data
        if not self._backlog:
            return
        leftover = self.pty.write(bytes(self._backlog))
        self._backlog = bytearray(leftover)
        self._arm_writer(bool(self._backlog))

    def _arm_writer(self, wanted: bool) -> None:
        if self._loop is None or self.pty.master_fd is None:
            return
        if wanted and not self._writer_armed:
            with contextlib.suppress(Exception):
                self._loop.add_writer(self.pty.master_fd, self._write_to_agy, b"")
                self._writer_armed = True
        elif not wanted and self._writer_armed:
            with contextlib.suppress(Exception):
                self._loop.remove_writer(self.pty.master_fd)
            self._writer_armed = False

    def _size(self) -> tuple[int, int]:
        return effective_size(self.local.size(), self.remote_cols, self.rc_enabled)

    def _apply_size(self) -> None:
        cols, rows = self._size()
        self.pty.set_winsize(cols, rows)
        self.screen.resize(cols, rows)
        self._debug(f"size -> {cols}x{rows} (rc={self.rc_enabled})")

    def _on_resize(self) -> None:
        self._apply_size()

    async def _force_repaint(self) -> None:
        """Nudge agy into redrawing, so the wrapper's own output is cleaned up
        and the emulated screen is populated straight after ``/rc on``."""
        cols, rows = self._size()
        self.pty.set_winsize(cols, max(rows - 1, 1))
        await asyncio.sleep(0.05)
        self.pty.set_winsize(cols, rows)

    async def _handle_local_command(self, command: str) -> None:
        if command == "/rc on":
            if self.rc_enabled:
                self.local.notice("[Dishell] 원격 제어는 이미 켜져 있습니다.")
            else:
                await self._start_bot()
        elif command == "/rc off":
            if not self.rc_enabled:
                self.local.notice("[Dishell] 원격 제어는 이미 꺼져 있습니다.")
            else:
                await self._stop_bot(announce="🔴 **원격 제어 종료**")
                self.local.notice("[Dishell] 원격 제어를 껐습니다.")
        elif command == "/rc status":
            state = "ON" if self.rc_enabled else "OFF"
            self.local.notice(f"[Dishell] 원격 제어: {state} · agy pid {self.pty.pid}")
        await self._force_repaint()

    # ----------------------------------------------------------- bot control

    async def _start_bot(self) -> None:
        if not self.token or not self.user_id:
            self.local.notice(
                "[Dishell] .env 의 DISCORD_TOKEN / DISCORD_USER_ID 가 설정되지 않았습니다."
            )
            return
        self.local.notice("[Dishell] 디스코드 봇 연결 중...")
        self.client = DishellBot(manager=self)
        self.bot_task = asyncio.create_task(self.client.start(self.token))

        # Race readiness against the login task: a bad token makes start()
        # raise, and waiting only on wait_until_ready() would sit out the full
        # timeout and then report "시간 초과" instead of the real reason.
        ready = asyncio.create_task(self.client.wait_until_ready())
        done, _ = await asyncio.wait(
            {ready, self.bot_task},
            timeout=READY_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if ready not in done:
            ready.cancel()
            reason = "시간 초과 (네트워크 또는 토큰 확인)"
            if self.bot_task in done:
                exc = self.bot_task.exception()
                if isinstance(exc, discord.LoginFailure):
                    reason = "토큰이 거부되었습니다. 포털에서 Reset Token 후 .env 갱신"
                elif isinstance(exc, discord.PrivilegedIntentsRequired):
                    reason = "포털에서 MESSAGE CONTENT INTENT 를 켜야 합니다"
                elif exc is not None:
                    reason = f"{type(exc).__name__}: {exc}"
            self._sticky(f"[Dishell] 디스코드 연결 실패 · {reason}")
            self._debug(f"start_bot failed: {reason}")
            await self._stop_bot()
            return

        # Prove the DM channel actually works before declaring success --
        # a bot with no mutual server cannot message its owner at all.
        if not await self._notify(
            "🟢 **원격 제어 시작** — 이 DM으로 로컬 agy 세션을 조작합니다. `/help` 로 사용법 확인."
        ):
            self._sticky("[Dishell] 연결됐지만 DM 을 보낼 수 없습니다 · 위 안내 참고")
            await self._stop_bot()
            return

        self.rc_enabled = True
        # Narrow agy so its lines survive Discord's code block width.
        self._apply_size()
        self.detector = AlertDetector()
        self._last_snapshot = None
        self._live_messages = []
        self._pending_alert = None
        self._start_new_message = True
        self.publish_task = asyncio.create_task(self._publish_loop())
        self.local.notice("[Dishell] 원격 제어를 켰습니다. 디스코드 DM으로 연결되었습니다.")

    async def _stop_bot(self, announce: str | None = None) -> None:
        if announce and self.rc_enabled:
            await self._notify(announce, color=discord.Color.red())
        was_enabled = self.rc_enabled
        self.rc_enabled = False
        if was_enabled:
            self._apply_size()  # give the local terminal its full width back

        if self.publish_task:
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

    def on_bot_ready(self, username: str) -> None:
        self.local.notice(f"[Dishell] 디스코드 로그인: {username}")

    # ------------------------------------------------------------- discord

    async def _get_user(self) -> discord.User | None:
        if self._user is not None:
            return self._user
        if not self.client or not self.user_id:
            return None
        self._user = self.client.get_user(self.user_id)
        if self._user is None:
            try:
                self._user = await self.client.fetch_user(self.user_id)
            except discord.NotFound:
                self.local.notice(
                    f"[Dishell] DISCORD_USER_ID {self.user_id} 에 해당하는 사용자가 없습니다."
                )
            except Exception as exc:
                self.local.notice(f"[Dishell] 사용자 조회 실패: {exc}")
        return self._user

    async def _notify(self, text: str, color: discord.Color | None = None) -> bool:
        user = await self._get_user()
        if user is None:
            return False
        embed = discord.Embed(description=text, color=color or discord.Color.green())
        try:
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            self.local.notice(FORBIDDEN_HELP)
            return False
        except Exception as exc:
            self.local.notice(f"[Dishell] DM 전송 실패: {exc}")
            return False

    def status_text(self) -> str:
        alive = self.pty.reap(block=False) is None
        state = "🟢 실행 중" if alive else "🔴 종료됨"
        return (
            f"agy pid `{self.pty.pid}` · {state} · "
            f"화면 {self.screen.cols}x{self.screen.rows}"
        )

    def remote_off(self) -> None:
        """Turn remote control off on behalf of Discord.

        The reply has to go out before the bot disconnects, so the teardown is
        scheduled rather than awaited.
        """
        self.local.notice("[Dishell] 디스코드에서 원격 제어를 껐습니다.")
        asyncio.create_task(self._stop_bot())
        asyncio.create_task(self._force_repaint())

    def send_input(self, payload: bytes) -> None:
        """Type ``payload`` into agy and post the next screen as a new message,
        so it lands underneath the command that caused it."""
        self._write_to_agy(payload)
        self._start_new_message = True

    async def handle_remote_message(self, message: discord.Message) -> None:
        action = parse_remote_message(message.content)

        async def reply(text: str) -> None:
            with contextlib.suppress(Exception):
                await message.reply(text, mention_author=False)

        if action.kind in ("help", "error"):
            await reply(action.message)
        elif action.kind == "rc_status":
            await reply(self.status_text())
        elif action.kind == "rc_off":
            await reply("원격 제어를 종료합니다.")
            self.remote_off()
        else:  # text / keys -> straight into agy's terminal
            self.send_input(action.payload)

    # ------------------------------------------------------------ publishing

    async def _publish_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if not self.rc_enabled:
                continue
            try:
                snapshot = self.screen.snapshot()
                # Fed every tick, including unchanged ones: "agy went quiet"
                # is itself a transition worth reporting.
                alert = self.detector.update(snapshot, time.monotonic())
                if alert:
                    self._pending_alert = alert
                    self._start_new_message = True

                if snapshot == self._last_snapshot and not self._start_new_message:
                    continue
                # Scrolled-off lines are only worth carrying into a fresh
                # message; an edit keeps showing the same live screen.
                history = self.screen.pop_history() if self._start_new_message else None
                await self._publish(snapshot, history)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.local.notice(f"[Dishell] 화면 전송 오류: {exc}")
                self._debug(f"publish failed: {exc!r}")

    def _frame(self, chunk: str, first: bool, last: bool) -> discord.Embed:
        embed = discord.Embed(
            title="agy · 실시간 화면" if first else None,
            description=chunk,
            color=discord.Color.blurple(),
        )
        if last:
            embed.set_footer(text="일반 메시지=입력 · /key esc · /rc off")
        return embed

    async def _publish(self, snapshot: str, history: list[str] | None = None) -> None:
        user = await self._get_user()
        if user is None:
            return

        chunks = render_for_discord(snapshot, history)
        header = ALERT_HEADERS.get(self._pending_alert or "", "")
        fresh = self._start_new_message or not self._live_messages

        try:
            if fresh:
                self._live_messages = []
            for i, chunk in enumerate(chunks):
                embed = self._frame(chunk, first=(i == 0), last=(i == len(chunks) - 1))
                if i < len(self._live_messages):
                    await self._live_messages[i].edit(embed=embed)
                else:
                    self._live_messages.append(
                        await user.send(
                            content=(header or None) if i == 0 else None,
                            embed=embed,
                        )
                    )

            # The screen shrank; drop the messages that are no longer covered.
            while len(self._live_messages) > len(chunks):
                with contextlib.suppress(discord.HTTPException):
                    await self._live_messages.pop().delete()

            self._start_new_message = False
            self._pending_alert = None

        except discord.Forbidden:
            self.local.notice(FORBIDDEN_HELP)
            self.local.notice("[Dishell] 원격 제어를 종료합니다.")
            asyncio.create_task(self._stop_bot())
            return
        except discord.NotFound:
            self._live_messages = []  # deleted by the user; start a fresh one
            self._start_new_message = True
            return
        except discord.HTTPException:
            # rate limited or network error; retry on the next tick
            return

        self._last_snapshot = snapshot

"""Discord adapters only. Every route uses the same policy and approval service."""

import contextlib

import discord
from discord import app_commands

from .audit import AuditError
from .commands import parse_remote_message, text_action
from .permissions import PermissionDenied, RemoteContext


def make_intents():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    return intents


def interaction_context(interaction):
    return RemoteContext(interaction.user.id,
        interaction.guild_id is None and isinstance(interaction.channel, discord.DMChannel))


class ApprovalView(discord.ui.View):
    def __init__(self, manager, request):
        super().__init__(timeout=max(0, request.expires_at - manager.approvals.clock()))
        self.manager, self.request = manager, request
        self.message = None
        for child in self.children:
            child.custom_id = f"dishell:{request.nonce}:{child.label}"

    async def _decide(self, interaction, approve):
        context = interaction_context(interaction)
        # Defer before filesystem/process operations; authorization still occurs
        # inside resolve_approval and never depends on whether this UI is visible.
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.manager.resolve_approval(context, self.request.nonce,
                                                        self.request.session_id, approve=approve)
        except Exception as exc:
            result = "승인 작업을 거부했습니다: " + (str(exc) if isinstance(exc, PermissionDenied) else type(exc).__name__)
        if context.user_id == self.manager.user_id and context.is_dm:
            for child in self.children:
                child.disabled = True
            self.stop()
            if self.message:
                with contextlib.suppress(discord.HTTPException):
                    await self.message.edit(view=self)
        await interaction.followup.send(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="승인", style=discord.ButtonStyle.danger)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, True)

    @discord.ui.button(label="거부", style=discord.ButtonStyle.secondary)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, False)

    async def on_timeout(self):
        try:
            self.manager.approvals.expire()
        except AuditError:
            self.manager._online = False
        for child in self.children:
            child.disabled = True
        if self.message:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


class DishellBot(discord.Client):
    def __init__(self, manager, **kwargs):
        super().__init__(intents=make_intents(), **kwargs)
        self.manager = manager
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    async def _dispatch(self, interaction, action):
        async def respond(text, view=None):
            kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
            if view is not None:
                kwargs["view"] = view
            if interaction.response.is_done():
                return await interaction.followup.send(text, wait=True, **kwargs)
            await interaction.response.send_message(text, **kwargs)
            return await interaction.original_response()
        await self.manager.handle_action(interaction_context(interaction), action, respond)

    def _register_commands(self):
        @self.tree.command(name="rc", description="원격 제어 상태 확인 / 종료 승인 요청")
        @app_commands.choices(action=[app_commands.Choice(name="off", value="off"),
                                      app_commands.Choice(name="status", value="status")])
        async def rc_command(interaction: discord.Interaction, action: str):
            await self._dispatch(interaction, parse_remote_message(f"/rc {action}"))

        @self.tree.command(name="key", description="특수키 입력 승인 요청")
        async def key_command(interaction: discord.Interaction, keys: str):
            await self._dispatch(interaction, parse_remote_message(f"/key {keys}"))

        @self.tree.command(name="say", description="활성 세션 텍스트 입력 승인 요청")
        async def say_command(interaction: discord.Interaction, text: str):
            await self._dispatch(interaction, text_action(text))

        @self.tree.command(name="help", description="사용법과 키 목록")
        async def help_command(interaction: discord.Interaction):
            await self._dispatch(interaction, parse_remote_message("/help"))

        @self.tree.command(name="sessions", description="세션 목록")
        async def sessions_command(interaction: discord.Interaction):
            await self._dispatch(interaction, parse_remote_message("/sessions"))

        group = app_commands.Group(name="session", description="CLI 세션 조회 및 관리")

        @group.command(name="start", description="허용된 CLI 시작 승인 요청")
        @app_commands.choices(cli=[app_commands.Choice(name=name, value=name)
                                  for name in ("agy", "codex", "claude", "opencode")])
        async def start(interaction: discord.Interaction, cli: str):
            await self._dispatch(interaction, parse_remote_message(f"/session start {cli}"))

        @group.command(name="attach", description="활성 원격 세션 전환 승인 요청")
        async def attach(interaction: discord.Interaction, id: str):
            await self._dispatch(interaction, parse_remote_message(f"/session attach {id}"))

        @group.command(name="status", description="세션 상태와 동기화 지표")
        async def status(interaction: discord.Interaction, id: str):
            await self._dispatch(interaction, parse_remote_message(f"/session status {id}"))

        @group.command(name="stop", description="세션 종료 승인 요청")
        async def stop(interaction: discord.Interaction, id: str):
            await self._dispatch(interaction, parse_remote_message(f"/session stop {id}"))

        @group.command(name="history", description="스크롤 기록 (0=최근, 페이지당 100행)")
        async def history(interaction: discord.Interaction, id: str, page: app_commands.Range[int, 0, 99999] = 0):
            await self._dispatch(interaction, parse_remote_message(f"/session history {id} {page}"))

        self.tree.add_command(group)

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        self.manager.on_bot_ready(str(self.user))

    async def on_resumed(self):
        self.manager.on_bot_ready(str(self.user))

    async def on_disconnect(self):
        self.manager.on_bot_disconnect()

    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        await self.manager.handle_remote_message(message)

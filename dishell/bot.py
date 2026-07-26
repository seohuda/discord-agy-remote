"""The Discord client. Every inbound DM is authorised before it reaches the PTY.

The same actions are reachable two ways: as plain DM text (``/key esc``) and as
real application slash commands, which Discord renders with autocompletion. Both
go through :mod:`dishell.commands`, so there is a single parser to keep honest.
"""

import discord
from discord import app_commands

from .commands import parse_remote_message, text_action


def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    return intents


class DishellBot(discord.Client):
    def __init__(self, manager, **kwargs):
        super().__init__(intents=make_intents(), **kwargs)
        self.manager = manager
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    # --------------------------------------------------------------- commands

    async def _authorised(self, interaction: discord.Interaction) -> bool:
        """Slash commands are usable by the configured owner only."""
        if interaction.user.id == self.manager.user_id:
            return True
        await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
        return False

    def _register_commands(self) -> None:
        @self.tree.command(name="rc", description="원격 제어 상태를 확인하거나 종료합니다.")
        @app_commands.describe(action="수행할 작업")
        @app_commands.choices(action=[
            app_commands.Choice(name="off (원격 제어 종료)", value="off"),
            app_commands.Choice(name="status (상태 확인)", value="status"),
        ])
        async def rc_command(interaction: discord.Interaction, action: str) -> None:
            if not await self._authorised(interaction):
                return
            parsed = parse_remote_message(f"/rc {action}")
            if parsed.kind == "rc_status":
                await interaction.response.send_message(
                    self.manager.status_text(), ephemeral=True
                )
            elif parsed.kind == "rc_off":
                await interaction.response.send_message(
                    "원격 제어를 종료합니다.", ephemeral=True
                )
                self.manager.remote_off()
            else:
                await interaction.response.send_message(parsed.message, ephemeral=True)

        @self.tree.command(name="key", description="특수키를 전송합니다.")
        @app_commands.describe(keys="전송할 키 이름 (예: esc, down down enter)")
        async def key_command(interaction: discord.Interaction, keys: str) -> None:
            if not await self._authorised(interaction):
                return
            parsed = parse_remote_message(f"/key {keys}")
            if parsed.kind == "error":
                await interaction.response.send_message(parsed.message, ephemeral=True)
                return
            await interaction.response.send_message(f"키 전송: `{keys}`", ephemeral=True)
            self.manager.send_input(parsed.payload)

        @self.tree.command(name="say", description="agy 입력창에 텍스트를 입력하고 전송합니다.")
        @app_commands.describe(text="보낼 내용")
        async def say_command(interaction: discord.Interaction, text: str) -> None:
            if not await self._authorised(interaction):
                return
            # Typed verbatim: a slash command argument is never a wrapper command.
            await interaction.response.send_message(f"전송: `{text}`", ephemeral=True)
            self.manager.send_input(text_action(text).payload)

        @self.tree.command(name="help", description="사용법과 키 목록을 표시합니다.")
        async def help_command(interaction: discord.Interaction) -> None:
            if not await self._authorised(interaction):
                return
            parsed = parse_remote_message("/help")
            await interaction.response.send_message(parsed.message, ephemeral=True)

    # ---------------------------------------------------------------- events

    async def setup_hook(self) -> None:
        # A global sync can take a while to propagate to the client, which is
        # why a freshly invited bot may need a Discord restart to see these.
        await self.tree.sync()

    async def on_ready(self):
        self.manager.on_bot_ready(str(self.user))

    async def on_message(self, message: discord.Message):
        if message.author.id == self.user.id:
            return
        # Remote input is accepted only in a DM from the single configured user.
        if not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.id != self.manager.user_id:
            return
        await self.manager.handle_remote_message(message)

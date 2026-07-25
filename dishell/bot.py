"""The Discord client. Every inbound DM is authorised before it reaches the PTY."""

import discord
from discord import app_commands


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

    async def setup_hook(self):
        from .commands import parse_remote_message
        import asyncio

        @self.tree.command(name="rc", description="원격 제어 상태를 확인하거나 종료합니다.")
        @app_commands.describe(action="수행할 작업")
        @app_commands.choices(action=[
            app_commands.Choice(name="off (원격 제어 종료)", value="off"),
            app_commands.Choice(name="status (상태 확인)", value="status")
        ])
        async def rc_cmd(interaction: discord.Interaction, action: str):
            if interaction.user.id != self.manager.user_id:
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            
            act = parse_remote_message(f"/rc {action}")
            if act.kind == "rc_status":
                alive = self.manager.pty.reap(block=False) is None
                state = "🟢 실행 중" if alive else "🔴 종료됨"
                await interaction.response.send_message(
                    f"agy pid `{self.manager.pty.pid}` · {state} · 화면 {self.manager.screen.cols}x{self.manager.screen.rows}",
                    ephemeral=True
                )
            elif act.kind == "rc_off":
                await interaction.response.send_message("원격 제어를 종료합니다.", ephemeral=True)
                self.manager.local.notice("[Dishell] 디스코드에서 원격 제어를 껐습니다.")
                asyncio.create_task(self.manager._stop_bot())
                asyncio.create_task(self.manager._force_repaint())
            else:
                await interaction.response.send_message(act.message, ephemeral=True)

        @self.tree.command(name="key", description="특수키를 전송합니다.")
        @app_commands.describe(keys="전송할 키 이름들 (예: esc, down down enter)")
        async def key_cmd(interaction: discord.Interaction, keys: str):
            if interaction.user.id != self.manager.user_id:
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            
            act = parse_remote_message(f"/key {keys}")
            if act.kind == "error":
                await interaction.response.send_message(act.message, ephemeral=True)
                return
                
            await interaction.response.send_message(f"키 전송: `{keys}`", ephemeral=True)
            self.manager._write_to_agy(act.payload)
            self.manager._start_new_message = True

        @self.tree.command(name="help", description="도움말을 표시합니다.")
        async def help_cmd(interaction: discord.Interaction):
            if interaction.user.id != self.manager.user_id:
                await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
                return
            act = parse_remote_message("/help")
            await interaction.response.send_message(act.message, ephemeral=True)

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

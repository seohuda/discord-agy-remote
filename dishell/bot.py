"""The Discord client. Every inbound DM is authorised before it reaches the PTY."""

import discord


def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    return intents


class DishellBot(discord.Client):
    def __init__(self, manager, **kwargs):
        super().__init__(intents=make_intents(), **kwargs)
        self.manager = manager

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

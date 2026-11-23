"""Slash command registration and routing, without touching the gateway."""

import asyncio
from unittest.mock import MagicMock

import discord
import pytest

from dishell.bot import DishellBot, make_intents

OWNER = 4242


class FakeResponse:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, user_id=OWNER, guild_id=None):
        self.user = type("User", (), {"id": user_id})()
        self.guild_id = guild_id
        self.response = FakeResponse()


class FakeManager:
    user_id = OWNER

    def __init__(self):
        self.inputs: list[bytes] = []
        self.turned_off = False
        self.handled_messages = []
        self.sync_errors: list[Exception] = []

    def status_text(self) -> str:
        return "status!"

    def remote_off(self) -> None:
        self.turned_off = True

    def send_input(self, payload: bytes) -> None:
        self.inputs.append(payload)

    async def handle_remote_message(self, message) -> None:
        self.handled_messages.append(message)

    def on_sync_error(self, exc: Exception) -> None:
        self.sync_errors.append(exc)


@pytest.fixture
def bot():
    manager = FakeManager()
    return DishellBot(manager=manager)


def invoke(bot, name: str, interaction, **kwargs):
    command = bot.tree.get_command(name)
    assert command is not None, f"{name} is not registered"
    asyncio.run(command.callback(interaction, **kwargs))


def test_message_content_intent_is_requested():
    """Without it the bot receives empty DMs and nothing works."""
    assert make_intents().message_content is True


def test_the_documented_commands_are_registered(bot):
    names = {command.name for command in bot.tree.get_commands()}
    assert names == {"rc", "key", "say", "help"}


def test_rc_status_reports_the_session(bot):
    interaction = FakeInteraction()
    invoke(bot, "rc", interaction, action="status")
    assert interaction.response.sent == [("status!", True)]


def test_rc_off_stops_remote_control(bot):
    invoke(bot, "rc", FakeInteraction(), action="off")
    assert bot.manager.turned_off is True


def test_key_sends_the_escape_sequence(bot):
    invoke(bot, "key", FakeInteraction(), keys="down enter")
    assert bot.manager.inputs == [b"\x1b[B\r"]


def test_an_unknown_key_is_reported_and_nothing_is_typed(bot):
    interaction = FakeInteraction()
    invoke(bot, "key", interaction, keys="banana")
    assert "banana" in interaction.response.sent[0][0]
    assert bot.manager.inputs == []


def test_say_types_the_text_verbatim_even_with_a_leading_slash(bot):
    invoke(bot, "say", FakeInteraction(), text="/model")
    assert bot.manager.inputs == [b"/model\r"]


def test_a_stranger_gets_nothing_through(bot):
    interaction = FakeInteraction(user_id=9)
    invoke(bot, "key", interaction, keys="enter")
    assert "권한" in interaction.response.sent[0][0]
    assert bot.manager.inputs == []


def test_slash_commands_are_rejected_outside_dms(bot):
    interaction = FakeInteraction(user_id=OWNER, guild_id=999)
    invoke(bot, "say", interaction, text="hello")
    assert "DM" in interaction.response.sent[0][0]
    assert bot.manager.inputs == []


def test_long_say_and_key_truncate_reply_preview_under_discord_limit(bot):
    long_text = "x" * 3000
    interaction = FakeInteraction()
    invoke(bot, "say", interaction, text=long_text)
    reply, ephemeral = interaction.response.sent[0]
    assert ephemeral is True
    assert len(reply) < 2000
    assert bot.manager.inputs == [long_text.encode("utf-8") + b"\r"]

    long_keys = " ".join(["down"] * 500)
    interaction2 = FakeInteraction()
    invoke(bot, "key", interaction2, keys=long_keys)
    assert len(interaction2.response.sent[0][0]) < 2000
    assert bot.manager.inputs[-1] == b"\x1b[B" * 500


def test_setup_hook_tolerates_tree_sync_http_error(bot, monkeypatch):
    async def fail_sync():
        resp = MagicMock(status=429, reason="Too Many Requests")
        raise discord.HTTPException(resp, "rate limited")

    monkeypatch.setattr(bot.tree, "sync", fail_sync)
    asyncio.run(bot.setup_hook())
    assert len(bot.manager.sync_errors) == 1


def test_on_message_accepts_only_owner_dms(bot, monkeypatch):
    bot_user = type("User", (), {"id": 1})()
    monkeypatch.setattr(type(bot), "user", property(lambda self: bot_user))

    dm_channel = MagicMock(spec=discord.DMChannel)
    guild_channel = object()

    def make_msg(author_id, channel):
        return type(
            "Msg",
            (),
            {"author": type("U", (), {"id": author_id})(), "channel": channel},
        )()

    asyncio.run(bot.on_message(make_msg(1, dm_channel)))         # self
    asyncio.run(bot.on_message(make_msg(OWNER, guild_channel)))  # guild
    asyncio.run(bot.on_message(make_msg(999, dm_channel)))       # stranger
    assert bot.manager.handled_messages == []

    owner_dm = make_msg(OWNER, dm_channel)
    asyncio.run(bot.on_message(owner_dm))
    assert bot.manager.handled_messages == [owner_dm]

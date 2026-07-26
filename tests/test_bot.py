"""Slash command registration and routing, without touching the gateway."""

import asyncio

import pytest

from dishell.bot import DishellBot, make_intents

OWNER = 4242


class FakeResponse:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, user_id=OWNER):
        self.user = type("User", (), {"id": user_id})()
        self.response = FakeResponse()


class FakeManager:
    user_id = OWNER

    def __init__(self):
        self.inputs: list[bytes] = []
        self.turned_off = False

    def status_text(self) -> str:
        return "status!"

    def remote_off(self) -> None:
        self.turned_off = True

    def send_input(self, payload: bytes) -> None:
        self.inputs.append(payload)


@pytest.fixture
def bot():
    manager = FakeManager()
    client = DishellBot(manager=manager)
    client.manager = manager
    return client


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

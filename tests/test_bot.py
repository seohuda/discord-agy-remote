"""Slash command registration and routing, without touching the gateway."""

import asyncio

import pytest
import discord
from unittest.mock import Mock

from dishell.audit import AuditLog
from dishell.manager import DishellManager
from dishell.permissions import RemoteContext
from dishell.sessions import Lifecycle

from dishell.bot import DishellBot, make_intents

OWNER = 4242


class FakeResponse:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []
        self.view = None

    def is_done(self):
        return bool(self.sent)

    async def send_message(self, content, ephemeral=False, **kwargs):
        self.sent.append((content, ephemeral))
        self.view = kwargs.get("view")


class FakeInteraction:
    def __init__(self, user_id=OWNER):
        self.user = type("User", (), {"id": user_id})()
        self.response = FakeResponse()
        self.guild_id = None
        self.channel = Mock(spec=discord.DMChannel)

    async def original_response(self):
        return None


@pytest.fixture
def bot(tmp_path):
    manager = DishellManager(token="mock", user_id=OWNER, argv=["agy"],
                             audit=AuditLog(tmp_path / "audit" / "audit.jsonl"))
    manager.inputs = []
    manager.turned_off = False
    manager.rc_enabled = manager._online = True
    manager.primary.state = Lifecycle.RUNNING
    manager.sessions.active_id = manager.primary.id
    manager.status_text = lambda: "status!"
    async def write(payload, **kwargs):
        manager.inputs.append(payload)
    manager.primary.write = write
    def remote_off():
        manager.turned_off = True
    manager.remote_off = remote_off
    return DishellBot(manager=manager)


def approve_pending(bot):
    request = next(iter(bot.manager.approvals.pending.values()))
    return asyncio.run(bot.manager.resolve_approval(RemoteContext(OWNER, True), request.nonce,
                                                   request.session_id, approve=True))


def invoke(bot, name: str, interaction, **kwargs):
    command = bot.tree.get_command(name)
    assert command is not None, f"{name} is not registered"
    asyncio.run(command.callback(interaction, **kwargs))


def test_message_content_intent_is_requested():
    """Without it the bot receives empty DMs and nothing works."""
    assert make_intents().message_content is True


def test_the_documented_commands_are_registered(bot):
    names = {command.name for command in bot.tree.get_commands()}
    assert names == {"rc", "key", "say", "help", "sessions", "session"}
    group = bot.tree.get_command("session")
    assert {c.name for c in group.commands} == {"start", "attach", "status", "stop", "history"}


def test_rc_status_reports_the_session(bot):
    interaction = FakeInteraction()
    invoke(bot, "rc", interaction, action="status")
    assert interaction.response.sent == [("status!", True)]


def test_rc_off_stops_remote_control(bot):
    invoke(bot, "rc", FakeInteraction(), action="off")
    assert bot.manager.turned_off is False
    approve_pending(bot)
    assert bot.manager.turned_off is True


def test_key_sends_the_escape_sequence(bot):
    invoke(bot, "key", FakeInteraction(), keys="down enter")
    assert bot.manager.inputs == []
    approve_pending(bot)
    assert bot.manager.inputs == [b"\x1b[B\r"]


def test_an_unknown_key_is_reported_and_nothing_is_typed(bot):
    interaction = FakeInteraction()
    invoke(bot, "key", interaction, keys="banana")
    assert "banana" in interaction.response.sent[0][0]
    assert bot.manager.inputs == []


def test_say_types_the_text_verbatim_even_with_a_leading_slash(bot):
    invoke(bot, "say", FakeInteraction(), text="/model")
    assert bot.manager.inputs == []
    approve_pending(bot)
    assert bot.manager.inputs == [b"/model\r"]


def test_a_stranger_gets_nothing_through(bot):
    interaction = FakeInteraction(user_id=9)
    invoke(bot, "key", interaction, keys="enter")
    assert "거부" in interaction.response.sent[0][0]
    assert bot.manager.inputs == []


def test_owner_slash_commands_in_public_channel_are_denied(bot):
    interaction = FakeInteraction()
    interaction.guild_id = 123
    invoke(bot, "say", interaction, text="must not type")
    assert "dm_required" in interaction.response.sent[0][0]
    assert not bot.manager.approvals.pending
    assert not bot.manager.inputs


def test_ai_approval_prompt_never_causes_automatic_enter(bot):
    from test_alerts import REAL_TRUST_PROMPT
    bot.manager.screen.feed(REAL_TRUST_PROMPT.encode())
    bot.manager.primary.observe()
    bot.manager.primary.observe()
    assert bot.manager.inputs == []
    invoke(bot, "key", FakeInteraction(), keys="enter")
    assert bot.manager.inputs == []
    approve_pending(bot)
    assert bot.manager.inputs == [b"\r"]


def test_screen_change_discards_previously_approved_input(bot):
    from dishell.permissions import PermissionDenied
    invoke(bot, "say", FakeInteraction(), text="safe at request time")
    bot.manager.screen.feed(b"new approval prompt")
    with pytest.raises(PermissionDenied, match="screen_changed"):
        approve_pending(bot)
    assert bot.manager.inputs == []
    assert not bot.manager.approvals.pending


def test_no_active_session_rejects_general_input(bot):
    bot.manager.sessions.active_id = None
    interaction = FakeInteraction()
    invoke(bot, "say", interaction, text="nowhere")
    assert "no_active_session" in interaction.response.sent[0][0]
    assert not bot.manager.inputs


def test_button_checks_owner_and_nonce_again(bot):
    async def scenario():
        interaction = FakeInteraction()
        await bot.tree.get_command("key").callback(interaction, keys="enter")
        view = interaction.response.view
        assert view is not None
        class Followup:
            async def send(self, *args, **kwargs):
                pass
        async def callback(user_id):
            callback_interaction = FakeInteraction(user_id)
            callback_interaction.followup = Followup()
            async def defer(**kwargs):
                pass
            callback_interaction.response.defer = defer
            await view.approve_button.callback(callback_interaction)
        await callback(9)
        assert not bot.manager.inputs
        await callback(OWNER)
        assert bot.manager.inputs == [b"\r"]
        await callback(OWNER)
        assert bot.manager.inputs == [b"\r"]
    asyncio.run(scenario())

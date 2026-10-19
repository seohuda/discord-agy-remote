"""Offline Discord callback/transport regressions; no live gateway is involved."""

import asyncio
import sys
from unittest.mock import Mock

import discord
import pytest
from test_bot import FakeInteraction
from test_remote_integration import manager_for, request

from dishell.bot import DishellBot
from dishell.commands import parse_remote_message
from dishell.events import EventDetector
from dishell.permissions import PermissionDenied, RemoteContext
from dishell.sessions import Lifecycle

OWNER = RemoteContext(42, True)


class Followup:
    def __init__(self):
        self.messages = []

    async def send(self, text, **kwargs):
        self.messages.append(text)


class ButtonInteraction(FakeInteraction):
    def __init__(self, user_id=42, *, guild_id=None, channel_type=discord.DMChannel, defer=None):
        super().__init__(user_id)
        self.guild_id = guild_id
        self.channel = Mock(spec=channel_type)
        self.followup = Followup()
        async def default_defer(**kwargs):
            pass
        self.response.defer = defer or default_defer


@pytest.fixture
def controller(tmp_path):
    manager = manager_for(tmp_path)
    manager.primary.state = Lifecycle.RUNNING
    manager.sessions.active_id = manager.primary.id
    writes, clock = [], [0.0]
    manager.approvals.clock = lambda: clock[0]
    async def write(payload, **kwargs):
        manager.primary.input_revision += 1
        writes.append((manager.primary.id, payload))
    # Spy at the PTY write boundary. Real PTYs are covered in test_sessions and
    # test_remote_integration; this fixture isolates callback races deterministically.
    manager.primary.write = write
    return manager, writes, clock


@pytest.mark.parametrize("user_id,guild_id,channel_type,reason", [
    (9, None, discord.DMChannel, "not_owner"),
    (42, 123, discord.DMChannel, "dm_required"),
    (42, None, discord.TextChannel, "dm_required"),
    (42, None, discord.GroupChannel, "dm_required"),
])
def test_unauthorized_buttons_never_write_or_consume_the_owner_request(controller, user_id, guild_id, channel_type, reason):
    async def scenario():
        manager, writes, _ = controller
        view = (await request(manager, "/say owner-approved-text")).views[0]
        assert not writes  # Read-only until an explicit owner confirmation.
        interaction = ButtonInteraction(user_id, guild_id=guild_id, channel_type=channel_type)
        await view.approve_button.callback(interaction)
        assert reason in interaction.followup.messages[0]
        assert view.request.nonce in manager.approvals.pending
        assert not any(child.disabled for child in view.children)
        assert not writes and not manager.primary._writes
        await view.approve_button.callback(ButtonInteraction())
        assert writes == [(manager.primary.id, b"owner-approved-text\r")]
    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["reject", "expire", "timeout"])
def test_rejection_and_expiration_discard_input_and_reject_button_reuse(controller, decision):
    async def scenario():
        manager, writes, clock = controller
        view = (await request(manager, "/key enter")).views[0]
        if decision == "reject":
            await view.reject_button.callback(ButtonInteraction())
        else:
            clock[0] = view.request.expires_at
            if decision == "timeout":
                await view.on_timeout()
            else:
                await view.approve_button.callback(ButtonInteraction())
        assert not writes and not manager.primary._writes
        assert view.request.nonce not in manager.approvals.pending
        replay = ButtonInteraction()
        await view.approve_button.callback(replay)
        assert "invalid_nonce_or_scope" in replay.followup.messages[0]
        assert not writes and not manager.primary._writes
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["screen", "input"])
def test_button_rechecks_screen_and_input_revisions(controller, changed):
    async def scenario():
        manager, writes, _ = controller
        view = (await request(manager, "/key enter")).views[0]
        if changed == "screen":
            manager.screen.feed(b"a new CLI prompt")
        else:
            manager.primary.input_revision += 1
        interaction = ButtonInteraction()
        await view.approve_button.callback(interaction)
        assert "screen_changed" in interaction.followup.messages[0]
        assert not writes and not manager.primary._writes
        assert not manager.approvals.pending
    asyncio.run(scenario())


def test_approval_cannot_be_used_for_another_session(controller):
    async def scenario():
        manager, writes, _ = controller
        other = manager.sessions.create("codex", argv=[sys.executable, "-c", "pass"])
        view = (await request(manager, "session-a-only")).views[0]
        with pytest.raises(PermissionDenied, match="invalid_nonce_or_scope"):
            await manager.resolve_approval(OWNER, view.request.nonce, other.id, approve=True)
        assert not writes and not other._writes
        await view.approve_button.callback(ButtonInteraction())
        assert writes == [(manager.primary.id, b"session-a-only\r")]
        assert not other._writes
    asyncio.run(scenario())


def test_gateway_disconnect_and_resume_invalidate_previous_buttons(controller):
    async def scenario():
        manager, writes, _ = controller
        bot = DishellBot(manager)
        old_view = (await request(manager, "old-input")).views[0]
        old_epoch = manager.approvals.epoch
        await bot.on_disconnect()
        assert not manager._online and not manager.approvals.pending
        await bot.on_resumed()
        assert manager._online and manager.approvals.epoch > old_epoch
        replay = ButtonInteraction()
        await old_view.approve_button.callback(replay)
        assert "invalid_nonce_or_scope" in replay.followup.messages[0]
        assert not writes
        new_view = (await request(manager, "fresh-input")).views[0]
        await new_view.approve_button.callback(ButtonInteraction())
        assert writes == [(manager.primary.id, b"fresh-input\r")]
    asyncio.run(scenario())


def test_concurrent_owner_replay_and_other_user_button_execute_at_most_once(controller):
    async def scenario():
        manager, writes, _ = controller
        view = (await request(manager, "/key enter")).views[0]
        ready, release, writing, finish_write = (asyncio.Event() for _ in range(4))
        arrived = 0
        async def defer(**kwargs):
            nonlocal arrived
            arrived += 1
            if arrived == 3:
                ready.set()
            await release.wait()
        async def blocked_write(payload, **kwargs):
            writes.append((manager.primary.id, payload))
            writing.set()
            await finish_write.wait()
        manager.primary.write = blocked_write
        interactions = [ButtonInteraction(user, defer=defer) for user in (42, 9, 42)]
        tasks = [asyncio.create_task(view.approve_button.callback(i)) for i in interactions]
        try:
            await asyncio.wait_for(ready.wait(), 1)
            release.set()
            await asyncio.wait_for(writing.wait(), 1)
            assert writes == [(manager.primary.id, b"\r")]
            assert not manager.approvals.pending
        finally:
            release.set()
            finish_write.set()
            await asyncio.gather(*tasks)
        assert len(writes) == 1
        assert "not_owner" in interactions[1].followup.messages[0]
        assert sum("invalid_nonce_or_scope" in i.followup.messages[0] for i in interactions) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/say unsent-input", "/session start codex"])
def test_confirmation_delivery_failure_revokes_nonce_and_start_reservation(controller, text):
    async def scenario():
        manager, writes, _ = controller
        views = []
        async def fail(text, view=None):
            if view:
                views.append(view)
            raise ConnectionError("test_transport_unavailable")
        with pytest.raises(ConnectionError):
            await manager.handle_action(OWNER, parse_remote_message(text), fail)
        assert not manager.approvals.pending
        assert manager.policy.sessions == set(manager.sessions.sessions)
        assert not writes and not manager.primary._writes
        with pytest.raises(PermissionDenied):
            await manager.resolve_approval(OWNER, views[0].request.nonce, views[0].request.session_id, approve=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/say cancelled-input", "/session start codex"])
def test_cancelled_confirmation_delivery_revokes_nonce_and_start_reservation(controller, text):
    async def scenario():
        manager, writes, _ = controller
        entered = asyncio.Event()
        views = []
        async def blocked_response(text, view=None):
            if view:
                views.append(view)
                entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(manager.handle_action(OWNER, parse_remote_message(text), blocked_response))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager.approvals.pending
        assert manager.policy.sessions == set(manager.sessions.sessions)
        assert not writes and not manager.primary._writes
        with pytest.raises(PermissionDenied):
            await manager.resolve_approval(OWNER, views[0].request.nonce, views[0].request.session_id, approve=True)
    asyncio.run(scenario())


def test_concurrent_notification_flushes_do_not_send_the_same_event_twice(controller):
    async def scenario():
        manager, _, _ = controller
        detector = EventDetector("codex-flush", manager.sessions.plugins["codex"])
        event = detector.update("", now=0, structured={"type": "turn.completed"})
        manager.sessions.events.put(event)
        sending, release, second_started = (asyncio.Event() for _ in range(3))
        sent = []
        async def notify(text):
            sent.append(text)
            sending.set()
            await release.wait()
            return True
        manager._notify = notify
        first = asyncio.create_task(manager._publish_events())
        await asyncio.wait_for(sending.wait(), 1)
        async def second_flush():
            second_started.set()
            await manager._publish_events()
        second = asyncio.create_task(second_flush())
        try:
            await asyncio.wait_for(second_started.wait(), 1)
            assert len(sent) == 1
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert len(sent) == 1
        assert len(manager.sessions.events) == 0
        assert not manager.sessions.events._keys
    asyncio.run(scenario())


def test_button_audit_failure_blocks_execution_and_consumes_nonce(controller, monkeypatch):
    from dishell.audit import AuditError
    async def scenario():
        manager, writes, _ = controller
        view = (await request(manager, "/key enter")).views[0]
        def fail(**kwargs):
            raise AuditError("disk_failed")
        monkeypatch.setattr(manager.audit, "record", fail)
        interaction = ButtonInteraction()
        await view.approve_button.callback(interaction)
        assert "AuditError" in interaction.followup.messages[0]
        assert not writes and not manager.primary._writes
        assert not manager.approvals.pending
    asyncio.run(scenario())


def test_cancelled_delivery_with_audit_failure_still_revokes_all_requests(controller, monkeypatch):
    from dishell.audit import AuditError
    async def scenario():
        manager, writes, _ = controller
        await request(manager, "another-pending-input")
        entered = asyncio.Event()
        async def blocked_response(text, view=None):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(manager.handle_action(OWNER, parse_remote_message("/session start codex"), blocked_response))
        await asyncio.wait_for(entered.wait(), 1)
        def fail(**kwargs):
            raise AuditError("disk_failed")
        monkeypatch.setattr(manager.audit, "record", fail)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager.approvals.pending
        assert not manager._online
        assert manager.policy.sessions == set(manager.sessions.sessions)
        assert not writes and not manager.primary._writes
    asyncio.run(scenario())


def test_cancelled_notification_delivery_keeps_event_and_releases_consumer_lock(controller):
    async def scenario():
        manager, _, _ = controller
        event = EventDetector("codex-cancel", manager.sessions.plugins["codex"]).update("", structured={"type": "turn.failed"})
        manager.sessions.events.put(event)
        entered = asyncio.Event()
        async def blocked_notify(text):
            entered.set()
            await asyncio.Event().wait()
        manager._notify = blocked_notify
        task = asyncio.create_task(manager._publish_events())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.sessions.events.peek() is event
        assert len(manager.sessions.events._keys) == 1
        assert not manager._event_publish_lock.locked()
        sent = []
        async def successful_notify(text):
            sent.append(text)
            return True
        manager._notify = successful_notify
        manager.sessions.events.put(event)  # Duplicate while retrying.
        await manager._publish_events()
        assert len(sent) == 1
        assert not manager.sessions.events._keys
        assert len(manager.sessions.events) == 0
    asyncio.run(scenario())

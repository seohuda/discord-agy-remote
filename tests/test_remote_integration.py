"""Exercise the real controller with Fake Discord and actual test-only PTYs."""
import asyncio
import sys
from unittest.mock import Mock

import discord
import pytest

from dishell.audit import AuditError, AuditLog
from dishell.commands import parse_remote_message
from dishell.manager import DishellManager
from dishell.permissions import PermissionDenied, RemoteContext
from dishell.plugins import default_plugins
from dishell.plugins.codex import CodexPlugin
from dishell.sessions import Lifecycle
from test_sessions import until

OWNER = RemoteContext(42, True)


class Responses:
    def __init__(self):
        self.texts = []
        self.views = []

    async def __call__(self, text, view=None):
        self.texts.append(text)
        if view:
            self.views.append(view)
        return None


def manager_for(tmp_path):
    mock = tmp_path / "mock-codex"
    mock.write_text("#!/usr/bin/env python3\nprint('SECOND',flush=True)\ninput()\n")
    mock.chmod(0o700)
    plugins = default_plugins()
    plugins["codex"] = CodexPlugin(str(mock))
    manager = DishellManager("mock", 42, [sys.executable, "-c", "print('FIRST',flush=True); input()"],
                             plugins=plugins, audit=AuditLog(tmp_path / "audit" / "audit.jsonl"))
    manager.local.write = lambda data: None
    manager.rc_enabled = manager._online = True
    return manager


async def request(manager, text, context=OWNER):
    replies = Responses()
    await manager.handle_action(context, parse_remote_message(text), replies)
    return replies


async def approve(manager, view):
    return await manager.resolve_approval(OWNER, view.request.nonce, view.request.session_id, approve=True)


def test_manage_start_attach_stop_all_require_scoped_owner_confirmation(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        await manager.sessions.launch(manager.primary, source="local")
        try:
            await until(lambda: "FIRST" in manager.screen.snapshot())
            denied = await request(manager, "/session start codex", RemoteContext(9, True))
            assert "not_owner" in denied.texts[0]
            assert len(manager.sessions.sessions) == 1
            replies = await request(manager, "/session start codex")
            assert len(manager.sessions.sessions) == 1
            start_view = replies.views[0]
            sid = start_view.request.session_id
            await approve(manager, start_view)
            second = manager.sessions.get(sid)
            await until(lambda: "SECOND" in second.screen.snapshot())
            assert manager.sessions.active_id == manager.primary.id
            attach_view = (await request(manager, f"/session attach {sid}")).views[0]
            assert manager.sessions.active_id == manager.primary.id
            await approve(manager, attach_view)
            assert manager.sessions.active_id == sid
            stop_view = (await request(manager, f"/session stop {sid}")).views[0]
            assert second.state == Lifecycle.RUNNING
            with pytest.raises(PermissionDenied):
                await manager.resolve_approval(RemoteContext(9, True), stop_view.request.nonce, sid, approve=True)
            assert second.state == Lifecycle.RUNNING
            await approve(manager, stop_view)
            assert second.pty.master_fd is None
            assert manager.sessions.active_id is None
        finally:
            await manager.sessions.close()
    asyncio.run(scenario())


def test_dm_guard_and_exactly_once_input_delivery_without_discord(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        await manager.sessions.launch(manager.primary, source="local")
        try:
            await until(lambda: "FIRST" in manager.screen.snapshot())
            writes = []
            original = manager.pty.write
            def write(data, **kwargs):
                writes.append(data)
                return original(data, **kwargs)
            manager.pty.write = write
            class Message:
                content = "한글\nsecond line"
                author = type("Author", (), {"id": 42})()
                channel = Mock(spec=discord.DMChannel)
                replies = Responses()
                async def reply(self, text, **kwargs):
                    return await self.replies(text, kwargs.get("view"))
            dm = Message()
            await manager.handle_remote_message(dm)
            assert writes == []
            view = dm.replies.views[0]
            expected = parse_remote_message(dm.content).payload
            await approve(manager, view)
            assert writes == [expected]
            with pytest.raises(PermissionDenied):
                await approve(manager, view)
            assert writes == [expected]
        finally:
            await manager.sessions.close()
    asyncio.run(scenario())


def test_audit_failure_prevents_real_pty_write(tmp_path, monkeypatch):
    async def scenario():
        manager = manager_for(tmp_path)
        await manager.sessions.launch(manager.primary, source="local")
        original_record = manager.audit.record
        try:
            await until(lambda: "FIRST" in manager.screen.snapshot())
            view = (await request(manager, "blocked")).views[0]
            writes = []
            manager.pty.write = lambda data, **kwargs: writes.append(data) or b""
            def fail(**kwargs):
                raise AuditError("disk_failed")
            monkeypatch.setattr(manager.audit, "record", fail)
            with pytest.raises(AuditError):
                await approve(manager, view)
            assert writes == []
            assert not manager.approvals.pending
        finally:
            monkeypatch.setattr(manager.audit, "record", original_record)
            await manager.sessions.close()
    asyncio.run(scenario())


def test_failed_notifications_survive_next_state_until_success(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        detector = manager.primary.detector
        plugin = manager.sessions.plugins["codex"]
        from dishell.events import EventDetector
        detector = EventDetector("codex-notify", plugin)
        first = detector.update("", now=1, structured={"type": "turn.completed"})
        second = detector.update("", now=2, structured={"type": "turn.failed", "error": {"message": "API_KEY=secret-fixture"}})
        manager.sessions.events.put(first)
        sent = []
        async def fail(text):
            return False
        manager._notify = fail
        await manager._publish_events()
        manager.sessions.events.put(second)
        assert len(manager.sessions.events) == 2
        async def succeed(text):
            sent.append(text)
            return True
        manager._notify = succeed
        manager._event_retry_at = 0
        await manager._publish_events()
        assert len(manager.sessions.events) == 0
        assert len(sent) == 2
        assert "ERROR" in sent[0]
        assert all("secret-fixture" not in text for text in sent)
        await manager.sessions.close()
    asyncio.run(scenario())


def test_session_publishers_keep_distinct_message_ids_and_session_headers(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        second = manager.sessions.create("codex", argv=[sys.executable, "-c", "pass"])
        from test_publish import FakeUser
        manager._user = FakeUser()
        manager.primary.screen.feed(b"FIRST")
        second.screen.feed(b"SECOND")
        for session in (manager.primary, second):
            publisher = manager._publisher(session)
            publisher.submit(session.diff.update(session.screen.capture()))
            await publisher.flush()
        a, b = manager.primary.publisher, second.publisher
        assert not set(a.messages) & set(b.messages)
        assert all(manager.primary.id in message.embed.title for message in a.messages)
        assert all(second.id in message.embed.title for message in b.messages)
        assert "SECOND" not in "".join(message.embed.description for message in a.messages)
        assert "FIRST" not in "".join(message.embed.description for message in b.messages)
        await manager.sessions.close()
    asyncio.run(scenario())


def test_post_write_audit_failure_revokes_all_further_remote_operations(tmp_path, monkeypatch):
    async def scenario():
        manager = manager_for(tmp_path)
        await manager.sessions.launch(manager.primary, source="local")
        original_record = manager.audit.record
        try:
            await until(lambda: "FIRST" in manager.screen.snapshot())
            view = (await request(manager, "authorized before disk failure")).views[0]
            writes = []
            manager.pty.write = lambda data, **kwargs: writes.append(data) or b""
            def fail_result(**kwargs):
                if kwargs["decision"] in ("executed", "failed"):
                    raise AuditError("result_log_failed")
                return original_record(**kwargs)
            monkeypatch.setattr(manager.audit, "record", fail_result)
            with pytest.raises(AuditError):
                await approve(manager, view)
            assert len(writes) == 1  # previously approved bytes cannot be undone
            assert not manager._online
            assert not manager.approvals.pending
            response = await request(manager, "must not run")
            assert "remote_disconnected" in response.texts[0]
            assert len(writes) == 1
        finally:
            monkeypatch.setattr(manager.audit, "record", original_record)
            await manager.sessions.close()
    asyncio.run(scenario())


def test_other_input_invalidates_preissued_enter_even_before_the_cli_redraws(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        await manager.sessions.launch(manager.primary, source="local")
        try:
            await until(lambda: "FIRST" in manager.screen.snapshot())
            view = (await request(manager, "/key enter")).views[0]
            version = manager.screen.version
            manager.primary.enqueue(b"local text")
            assert manager.screen.version == version  # no event-loop yield / redraw yet
            with pytest.raises(PermissionDenied, match="screen_changed"):
                await approve(manager, view)
        finally:
            await manager.sessions.close()
    asyncio.run(scenario())


def test_primary_process_completion_notification_is_flushed_before_disconnect(tmp_path):
    async def scenario():
        from test_publish import FakeUser
        manager = DishellManager("mock", 42, [sys.executable, "-c", "print('done',flush=True)"],
                                 audit=AuditLog(tmp_path / "audit" / "audit.jsonl"))
        user = FakeUser()
        manager._user = user
        manager.rc_enabled = manager._online = True
        manager.local.write = lambda data: None
        assert await manager.run() == 0
        texts = [message.embed.description for message in user.messages]
        assert any("COMPLETED" in text and manager.primary.id in text for text in texts)
        assert manager.pty.master_fd is None
    asyncio.run(scenario())


def test_rejected_oversized_input_attempt_is_audited_without_its_content(tmp_path):
    async def scenario():
        manager = manager_for(tmp_path)
        manager.primary.state = Lifecycle.RUNNING
        manager.sessions.active_id = manager.primary.id
        marker = "never_log_the_prompt_"
        response = await request(manager, marker * 4000)
        assert "invalid_request" in response.texts[0]
        assert not manager.approvals.pending
        content = manager.audit.path.read_text()
        assert '"action": "remote_input"' in content
        assert '"decision": "attempted"' in content
        assert marker not in content
        # No child was started by this controller-only test.
        manager.primary.state = Lifecycle.CREATED
        await manager.sessions.close()
    asyncio.run(scenario())

"""The publishing path, remote message routing, and bot lifecycle guards."""

import asyncio
from unittest.mock import MagicMock

import discord
import pytest

from dishell.manager import DishellManager, _int_env


class FakeMessage:
    def __init__(self, content, embed):
        self.content = content
        self.embed = embed
        self.deleted = False
        self.edits = 0

    async def edit(self, embed=None):
        self.embed = embed
        self.edits += 1

    async def delete(self):
        self.deleted = True


class FakeUser:
    def __init__(self):
        self.messages: list[FakeMessage] = []

    async def send(self, content=None, embed=None):
        message = FakeMessage(content, embed)
        self.messages.append(message)
        return message


@pytest.fixture
def manager():
    manager = DishellManager(token="t", user_id=1, argv=["agy"])
    manager._user = FakeUser()
    return manager


def run(coro):
    return asyncio.run(coro)


def test_the_first_screen_is_sent_as_a_new_message(manager):
    run(manager._publish("hello"))
    assert len(manager._user.messages) == 1
    assert "hello" in manager._user.messages[0].embed.description


def test_a_following_screen_edits_the_same_message(manager):
    run(manager._publish("first"))
    run(manager._publish("second"))
    assert len(manager._user.messages) == 1
    assert manager._user.messages[0].edits == 1
    assert "second" in manager._user.messages[0].embed.description


def test_an_alert_starts_a_new_message_with_a_header(manager):
    run(manager._publish("first"))
    manager._start_new_message = True
    manager._pending_alert = "approval"
    run(manager._publish("waiting"))
    assert len(manager._user.messages) == 2
    assert "입력 대기" in manager._user.messages[1].content
    # The flags are one-shot: the next screen goes back to editing.
    assert manager._pending_alert is None
    run(manager._publish("still waiting"))
    assert len(manager._user.messages) == 2


def test_growing_output_adds_messages_and_shrinking_removes_them(manager):
    long_screen = "\n".join(f"line {i}" for i in range(2000))
    run(manager._publish(long_screen))
    assert len(manager._user.messages) > 1
    grew = list(manager._user.messages)

    run(manager._publish("short"))
    assert all(m.deleted for m in grew[1:])
    assert not grew[0].deleted
    assert len(manager._live_messages) == 1


def test_history_lines_are_published_above_the_screen(manager):
    run(manager._publish("now", ["scrolled off"]))
    description = manager._user.messages[0].embed.description
    assert description.index("scrolled off") < description.index("now")


def _http_error(status: int = 429):
    resp = MagicMock(status=status, reason="Error")
    return discord.HTTPException(resp, "fail")


def test_partial_multi_chunk_failure_does_not_duplicate_alert(manager):
    long_screen = "\n".join(f"line {i}" for i in range(2000))
    calls = 0
    original_send = manager._user.send

    async def flaky_send(content=None, embed=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise _http_error()
        return await original_send(content=content, embed=embed)

    manager._user.send = flaky_send
    manager._start_new_message = True
    manager._pending_alert = "approval"
    run(manager._publish(long_screen))

    # Chunk 0 succeeded, so the alert and fresh flag were cleared immediately.
    assert len(manager._user.messages) == 1
    assert manager._pending_alert is None
    assert manager._start_new_message is False

    # Retry finishes remaining chunks without sending another alert message.
    run(manager._publish(long_screen))
    alert_msgs = [m for m in manager._user.messages if m.content]
    assert len(alert_msgs) == 1
    assert len(manager._user.messages) > 1


def test_first_chunk_failure_keeps_alert_and_pending_history_for_retry(manager):
    async def fail_send(content=None, embed=None):
        raise _http_error()

    original_send = manager._user.send
    manager._user.send = fail_send
    manager._start_new_message = True
    manager._pending_alert = "done"
    manager._pending_history = ["older line"]
    run(manager._publish("screen", list(manager._pending_history)))

    assert manager._start_new_message is True
    assert manager._pending_alert == "done"
    assert manager._pending_history == ["older line"]

    manager._user.send = original_send
    run(manager._publish("screen", list(manager._pending_history)))
    assert len(manager._user.messages) == 1
    assert "작업 완료" in manager._user.messages[0].content
    assert "older line" in manager._user.messages[0].embed.description
    assert manager._pending_history == []


def test_not_found_resets_live_messages_and_starts_fresh(manager):
    run(manager._publish("first"))

    async def missing_edit(embed=None):
        resp = MagicMock(status=404, reason="Not Found")
        raise discord.NotFound(resp, "Unknown Message")

    manager._live_messages[0].edit = missing_edit
    run(manager._publish("second"))
    assert manager._live_messages == []
    assert manager._start_new_message is True


def test_forbidden_during_publish_stops_remote_control(manager):
    async def run_test():
        stopped = False

        async def fake_stop():
            nonlocal stopped
            stopped = True

        async def forbid_send(content=None, embed=None):
            resp = MagicMock(status=403, reason="Forbidden")
            raise discord.Forbidden(resp, "Cannot send messages to this user")

        manager._stop_bot = fake_stop
        manager._user.send = forbid_send
        await manager._publish("hello")
        await asyncio.sleep(0)
        assert stopped is True

    run(run_test())


def test_handle_remote_message_routes_commands_and_text(manager):
    replies: list[str] = []
    sent_payloads: list[bytes] = []
    off_called = False

    class FakeDM:
        def __init__(self, content):
            self.content = content

        async def reply(self, text, mention_author=False):
            replies.append(text)

    manager.send_input = lambda payload: sent_payloads.append(payload)
    manager.status_text = lambda: "status ok"

    def fake_off():
        nonlocal off_called
        off_called = True

    manager.remote_off = fake_off

    run(manager.handle_remote_message(FakeDM("/help")))
    run(manager.handle_remote_message(FakeDM("/rc status")))
    run(manager.handle_remote_message(FakeDM("/say /model")))
    run(manager.handle_remote_message(FakeDM("/key esc")))
    run(manager.handle_remote_message(FakeDM("/rc off")))

    assert any("discord-agy-remote" in r for r in replies)
    assert "status ok" in replies
    assert any("종료" in r for r in replies)
    assert sent_payloads == [b"/model\r", b"\x1b"]
    assert off_called is True


def test_start_bot_rejects_concurrent_starts_and_resets_screen(monkeypatch):
    import dishell.manager as mgr_mod

    started = 0

    async def run_test():
        ready_gate = asyncio.Event()
        stop_gate = asyncio.Event()

        class StubBot:
            def __init__(self, manager):
                nonlocal started
                started += 1
                self.manager = manager
                self.closed = False

            async def start(self, token):
                await stop_gate.wait()

            async def wait_until_ready(self):
                await ready_gate.wait()

            async def close(self):
                self.closed = True
                stop_gate.set()

        monkeypatch.setattr(mgr_mod, "DishellBot", StubBot)
        m = DishellManager(token="tok", user_id=1, argv=["agy"])
        m.screen.feed(b"stale output\r\n")

        async def fake_notify(*args, **kwargs):
            return True

        m._notify = fake_notify
        m._publish_loop = lambda: asyncio.sleep(60)

        t1 = asyncio.create_task(m._start_bot())
        await asyncio.sleep(0)
        assert m._starting is True

        # A second /rc on while connecting must not spawn a duplicate client.
        await m._handle_local_command("/rc on")
        assert started == 1
        assert "진행 중" in m._sticky_text

        ready_gate.set()
        await t1
        assert m.rc_enabled is True
        assert m.screen.snapshot() == ""  # stale screen cleared on connect

        await m._stop_bot()

    run(run_test())


def test_unexpected_bot_exit_disables_remote_control(monkeypatch):
    import dishell.manager as mgr_mod

    async def run_test():
        crash_gate = asyncio.Event()

        class CrashingBot:
            def __init__(self, manager):
                self.manager = manager

            async def start(self, token):
                await crash_gate.wait()
                raise RuntimeError("gateway lost")

            async def wait_until_ready(self):
                return

            async def close(self):
                return

        monkeypatch.setattr(mgr_mod, "DishellBot", CrashingBot)
        m = DishellManager(token="tok", user_id=1, argv=["agy"])

        async def fake_notify(*args, **kwargs):
            return True

        m._notify = fake_notify
        m._publish_loop = lambda: asyncio.sleep(60)

        await m._start_bot()
        assert m.rc_enabled is True

        crash_gate.set()
        await asyncio.sleep(0.05)
        assert m.rc_enabled is False
        assert "끊김" in m._sticky_text

    run(run_test())


def test_int_env_handles_malformed_values(monkeypatch):
    for bad in ("", "--5", "-", "abc", "²"):
        monkeypatch.setenv("DISHELL_TEST_INT", bad)
        assert _int_env("DISHELL_TEST_INT", 60) == 60
    monkeypatch.setenv("DISHELL_TEST_INT", "50")
    assert _int_env("DISHELL_TEST_INT", 60) == 50

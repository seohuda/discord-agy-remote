import asyncio
import os
import sys

import pytest

from dishell.audit import AuditLog
from dishell.permissions import SecurityPolicy
from dishell.plugins import default_plugins
from dishell.sessions import Lifecycle, SessionError, SessionManager
from dishell.launch import LaunchError


def make_manager(tmp_path, **kwargs):
    audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
    return SessionManager(default_plugins(), SecurityPolicy(1, audit), audit, **kwargs)


async def until(predicate, timeout=4):
    async def wait():
        while not predicate():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), timeout)


def test_two_real_mock_cli_ptys_remain_independent_and_attach_routes_input(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        code = "import sys; print(sys.argv[1], flush=True); print('reply', input(), flush=True)"
        a = manager.create("codex", argv=[sys.executable, "-c", code, "FIRST"])
        b = manager.create("claude", argv=[sys.executable, "-c", code, "SECOND"])
        try:
            await asyncio.gather(manager.launch(a), manager.launch(b))
            await until(lambda: "FIRST" in a.screen.snapshot() and "SECOND" in b.screen.snapshot())
            assert "SECOND" not in a.screen.snapshot()
            assert "FIRST" not in b.screen.snapshot()
            manager.attach(b.id)
            await manager.get(manager.active_id).write(b"target\r")
            await until(lambda: b.state != Lifecycle.RUNNING)
            assert "reply target" in b.screen.snapshot()
            assert "target" not in a.screen.snapshot()
            with pytest.raises(SessionError):
                await b.write(b"denied\r")
            assert b.pty.master_fd is None
        finally:
            await manager.close()
        assert a.pty.master_fd is None
        assert a.pty.reap(False) is not None
    asyncio.run(scenario())


def test_capacity_closed_manager_and_invalid_session(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path, max_sessions=2)
        for plugin in ("agy", "codex"):
            manager.create(plugin, argv=[sys.executable, "-c", "pass"])
        with pytest.raises(SessionError, match="maximum"):
            manager.create("claude", argv=[sys.executable])
        with pytest.raises(SessionError, match="invalid_session"):
            manager.attach("missing")
        await manager.close()
        with pytest.raises(SessionError, match="closed"):
            manager.create("agy", argv=[sys.executable])
    asyncio.run(scenario())


def test_plugins_are_allowlisted_missing_cli_is_helpful_and_argv_is_never_shell(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(LaunchError, match="PATH"):
        manager.create("codex")
    with pytest.raises(LaunchError):
        manager.new_id("/bin/sh")
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    argv = manager.plugins["codex"].build_argv(["literal; touch /tmp/never"])
    assert argv == [str(executable), "literal; touch /tmp/never"]


def test_shutdown_kills_stubborn_process_and_reaps_without_blocking_loop(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        session = manager.create("agy", argv=[sys.executable, "-c", code])
        await manager.launch(session)
        await until(lambda: "ready" in session.screen.snapshot())
        ticks = []
        async def heartbeat():
            for _ in range(20):
                ticks.append(1)
                await asyncio.sleep(.01)
        await asyncio.gather(manager.close(), heartbeat())
        assert len(ticks) == 20
        assert session.exit_code == 137
        assert session.pty.master_fd is None
        with pytest.raises(ProcessLookupError):
            os.kill(session.pty.pid, 0)
    asyncio.run(scenario())


def test_locally_selected_json_mode_is_parsed_but_plain_output_is_not(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        code = 'print(\'{"type":"turn.completed","usage":{}}\', flush=True); input()'
        a = manager.create("codex", argv=[sys.executable, "-c", code], args=["exec", "--json"])
        b = manager.create("codex", argv=[sys.executable, "-c", code])
        try:
            await manager.launch(a)
            await manager.launch(b)
            await until(lambda: "turn.completed" in a.screen.snapshot() and "turn.completed" in b.screen.snapshot())
            a.observe()
            b.observe()
            assert a.detector.state.value == "COMPLETED"
            assert b.detector.state.value == "UNKNOWN"
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_cancellation_during_stop_still_reaps_and_closes_pty(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        session = manager.create("codex", argv=[sys.executable, "-c", code])
        await manager.launch(session)
        await until(lambda: "ready" in session.screen.snapshot())
        task = asyncio.create_task(session.stop())
        await asyncio.sleep(.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.pty.master_fd is None
        assert session.pty.reap(False) is not None
        await manager.close()
    asyncio.run(scenario())


def test_remote_backpressure_buffer_is_discarded_on_revoke_or_timeout(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        session = manager.create("codex", argv=[sys.executable])
        session.state = Lifecycle.RUNNING
        class FakeLoop:
            def create_future(self):
                return asyncio.get_running_loop().create_future()
            def add_writer(self, *args):
                pass
            def remove_writer(self, *args):
                pass
        class BlockedPTY:
            master_fd = 999
            def reap(self, **kwargs):
                return None
            def write(self, data, **kwargs):
                return data
        session._loop = FakeLoop()
        session.pty = BlockedPTY()
        task = asyncio.create_task(session.write(b"must not outlive connection\r"))
        await asyncio.sleep(0)
        assert session._writes
        session.cancel_remote_writes()
        with pytest.raises(SessionError, match="revoked"):
            await task
        assert not session._writes
        with pytest.raises(asyncio.TimeoutError):
            await session.write(b"must not outlive ttl\r", timeout=.01)
        assert not session._writes
        assert not session._writer_armed
    asyncio.run(scenario())

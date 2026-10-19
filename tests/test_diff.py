import asyncio

from dishell.diff import BlockPublisher, ScreenDiffEngine, render_blocks, utf16_len
from dishell.screen import ScreenFrame, TerminalScreen


class Transport:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
        self.failure = None
        self.on_edit = None

    async def send(self, index, text):
        if self.failure:
            raise self.failure
        message = object()
        self.sent.append((message, index, text))
        return message

    async def edit(self, message, index, text):
        if self.failure:
            raise self.failure
        self.edited.append((message, index, text))
        if self.on_edit:
            callback, self.on_edit = self.on_edit, None
            callback()

    async def delete(self, message):
        self.deleted.append(message)


def frame(lines=None):
    return ScreenFrame(tuple(lines or [f"row {i}" for i in range(24)]), 60, 24)


def test_identical_frame_never_edits_and_one_row_only_edits_its_block():
    async def scenario():
        t = Transport()
        p, d = BlockPublisher(t), ScreenDiffEngine()
        first = frame()
        p.submit(d.update(first))
        await p.flush()
        for _ in range(10):
            p.submit(d.update(first))
            await p.flush()
        assert not t.edited
        changed = list(first.lines)
        changed[9] = "new 한글"
        patch = d.update(frame(changed))
        assert patch.changed_rows[0].row == 9
        p.submit(patch)
        await p.flush()
        assert [e[1] for e in t.edited] == [1]
        changed[1], changed[21] = "top", "bottom"
        p.submit(d.update(frame(changed)))
        await p.flush()
        assert [e[1] for e in t.edited] == [1, 0, 2]
    asyncio.run(scenario())


def test_character_regions_include_erased_cells_and_deleted_rows():
    d = ScreenDiffEngine()
    d.update(ScreenFrame(("abcdef", "deleted"), 20, 2))
    patch = d.update(ScreenFrame(("abc", ""), 20, 2))
    assert (patch.changed_rows[0].start, patch.changed_rows[0].end) == (3, 6)
    assert patch.changed_rows[1].after == ""


def test_ansi_unicode_partial_utf8_and_clear_resize_scroll():
    screen, d = TerminalScreen(30, 4), ScreenDiffEngine()
    d.update(screen.capture())
    data = "\x1b[31m한글🙂\x1b[0m".encode()
    for byte in data:
        screen.feed(bytes([byte]))
    patch = d.update(screen.capture())
    assert patch.changed_rows[0].after == "한글🙂"
    count = screen.snapshot_count
    assert screen.capture() is screen.capture()
    assert screen.snapshot_count == count
    screen.feed(b"\x1b[2J")
    assert d.update(screen.capture()).reset
    screen.resize(40, 5)
    assert d.update(screen.capture()).resized
    screen.feed(b"\r\n" * 20)
    assert d.update(screen.capture()).scrolled


def test_large_repaint_is_full_refresh_and_worst_case_embeds_fit():
    d = ScreenDiffEngine()
    d.update(frame())
    assert d.update(frame(["new" for _ in range(24)])).full_refresh
    f = ScreenFrame(tuple("🙂" * 120 for _ in range(60)), 240, 60)
    blocks = render_blocks(f)
    assert len(blocks) <= 16
    assert all(utf16_len(b) < 3900 for b in blocks)
    assert sum(b.count("🙂") for b in blocks) == 120 * 60


def test_rate_limit_retains_latest_revision_and_reconnect_restores_all_blocks():
    class RateLimit(Exception):
        status = 429
        retry_after = 5
    async def scenario():
        now = [0.0]
        t = Transport()
        p, d = BlockPublisher(t, clock=lambda: now[0]), ScreenDiffEngine()
        p.submit(d.update(frame(), now=0))
        t.failure = RateLimit()
        assert not await p.flush()
        t.failure = None
        p.submit(d.update(frame(["latest"] * 24), now=1))
        assert not await p.flush()
        now[0] = 6
        assert await p.flush()
        assert all("latest" in x[2] for x in t.sent)
        old_count = len(t.sent)
        p.reconnect()
        p.submit(d.update(frame(["latest"] * 24), now=6))
        await p.flush()
        assert len(t.sent) == old_count * 2
    asyncio.run(scenario())


def test_changes_during_send_cannot_overwrite_newer_revision():
    async def scenario():
        t = Transport()
        p, d = BlockPublisher(t), ScreenDiffEngine()
        p.submit(d.update(frame()))
        await p.flush()
        old = d.update(frame(["old"] * 24))
        new = d.update(frame(["new"] * 24))
        t.on_edit = lambda: p.submit(new)
        p.submit(old)
        await p.flush()
        assert p.applied_revision == new.revision
        assert all("new" in text for text in p._contents)
        p.submit(old)
        await p.flush()
        assert p.applied_revision == new.revision
    asyncio.run(scenario())


def test_reconnect_during_inflight_send_does_not_keep_old_message():
    async def scenario():
        t = Transport()
        p, d = BlockPublisher(t), ScreenDiffEngine()
        original = t.send
        async def reconnect(index, text):
            p.reconnect()
            t.send = original
            return await original(index, text)
        t.send = reconnect
        patch = d.update(frame())
        p.submit(patch)
        assert not await p.flush()
        assert not p.messages
        p.submit(patch)
        await p.flush()
        assert len(p.messages) == 3
    asyncio.run(scenario())


def test_reconnect_during_edit_fences_old_contents_without_index_error():
    async def scenario():
        t = Transport()
        p, d = BlockPublisher(t), ScreenDiffEngine()
        p.submit(d.update(frame()))
        await p.flush()
        patch = d.update(frame(["changed"] * 24))
        t.on_edit = p.reconnect
        p.submit(patch)
        assert not await p.flush()
        assert p.metrics.failures == 0
        p.submit(patch)
        await p.flush()
        assert p.applied_revision == patch.revision
        assert len(p.messages) == 3
    asyncio.run(scenario())


def test_pathological_combining_marks_do_not_prevent_other_rows_from_syncing():
    screen = TerminalScreen(60, 24)
    screen.feed(("x" + "\u0301" * 5000 + "\r\nnext row").encode())
    blocks = render_blocks(screen.capture())
    assert all(utf16_len(b) < 3900 for b in blocks)
    assert "행 생략" in blocks[0]
    assert "next row" in blocks[0]


class RecordingTransport(Transport):
    """Record the fake Discord side, independently of publisher._contents."""
    def __init__(self):
        super().__init__()
        self.visible = {}

    async def send(self, index, text):
        message = await super().send(index, text)
        self.visible[message] = text
        return message

    async def edit(self, message, index, text):
        await super().edit(message, index, text)
        self.visible[message] = text

    async def delete(self, message):
        await super().delete(message)
        self.visible.pop(message)


def assert_visible_frame(transport, publisher, screen_frame):
    assert [transport.visible[message] for message in publisher.messages] == render_blocks(screen_frame)


def test_visible_discord_blocks_remain_consistent_through_scroll_resize_retry_and_reconnect():
    async def scenario():
        now = [0.0]
        t = RecordingTransport()
        p, d, screen = BlockPublisher(t, clock=lambda: now[0]), ScreenDiffEngine(), TerminalScreen(60, 24)
        for row in range(24):
            screen.feed(f"\x1b[{row+1};1Hrow {row} 한글🙂".encode())
        original = screen.capture()
        p.submit(d.update(original, now=0))
        assert await p.flush()
        assert_visible_frame(t, p, original)
        p.submit(d.update(original, now=0))
        assert await p.flush()
        assert not t.edited
        screen.feed("\x1b[10;1H\x1b[2K한 행 변경".encode())
        p.submit(d.update(screen.capture(), now=0))
        assert await p.flush()
        assert [entry[1] for entry in t.edited] == [1]
        assert_visible_frame(t, p, screen.capture())
        screen.feed("\x1b[24;1H\r\n스크롤 다음 행".encode())
        patch = d.update(screen.capture(), now=0)
        assert patch.scrolled and patch.full_refresh
        p.submit(patch)
        assert await p.flush()
        assert_visible_frame(t, p, screen.capture())
        screen.resize(80, 12)
        patch = d.update(screen.capture(), now=0)
        assert patch.resized and patch.full_refresh
        p.submit(patch)
        assert await p.flush()
        assert_visible_frame(t, p, screen.capture())
        assert len(t.visible) == len(p.messages) == 2
        screen.feed(b"\x1b[1;1H\x1b[2Kfailed-old-frame")
        old = d.update(screen.capture(), now=0)
        t.failure = ConnectionError("offline")
        p.submit(old)
        assert not await p.flush()
        screen.feed(b"\x1b[1;1H\x1b[2K")
        for byte in "최신🙂".encode():
            screen.feed(bytes([byte]))
        latest = screen.capture()
        p.submit(d.update(latest, now=1))
        t.failure, now[0] = None, 3
        assert await p.flush()
        assert_visible_frame(t, p, latest)
        edits = len(t.edited)
        p.submit(old)
        assert await p.flush()
        assert len(t.edited) == edits
        assert_visible_frame(t, p, latest)
        p.reconnect()
        p.submit(d.update(latest, now=3))
        assert await p.flush()
        assert_visible_frame(t, p, latest)
    asyncio.run(scenario())


def test_cancelled_inflight_send_restores_the_complete_latest_frame():
    import pytest
    async def scenario():
        t = RecordingTransport()
        p, d = BlockPublisher(t), ScreenDiffEngine()
        entered = asyncio.Event()
        original_send = t.send
        async def delivered_but_not_returned(index, text):
            message = await original_send(index, text)
            entered.set()
            await asyncio.Event().wait()
            return message
        t.send = delivered_but_not_returned
        p.submit(d.update(frame()))
        task = asyncio.create_task(p.flush())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not p.messages
        assert not p._lock.locked()
        t.send = original_send
        latest = frame(["최신 한글" for _ in range(24)])
        p.submit(d.update(latest))
        assert await p.flush()
        assert_visible_frame(t, p, latest)
        assert len(p.messages) == 3
    asyncio.run(scenario())

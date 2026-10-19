"""Screen patches and a transport-independent, latest-revision block publisher.

Discord edits replace whole embeds. Blocks are positioned by terminal row, never
by the amount of nonblank text. No character-level network savings are claimed.
All methods belong to the owning asyncio loop.
"""

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Callable

from .screen import DISCORD_LIMIT, ScreenFrame

MAX_BLOCKS = 16
MAX_COLS = 240
MAX_ROWS = 60


@dataclass(frozen=True)
class RowChange:
    row: int
    start: int
    end: int
    before: str
    after: str


@dataclass(frozen=True)
class ScreenPatch:
    revision: int
    changed_rows: tuple[RowChange, ...]
    full_refresh: bool
    timestamp: float
    frame: ScreenFrame
    scrolled: bool = False
    resized: bool = False
    reset: bool = False


@dataclass
class SyncMetrics:
    snapshots: int = 0
    discord_edits: int = 0
    discord_sends: int = 0
    changed_rows: int = 0
    changed_blocks: int = 0
    duplicate_updates: int = 0
    full_refreshes: int = 0
    queue_wait_seconds: float = 0.0
    payload_utf16_units: int = 0
    failures: int = 0


class ScreenDiffEngine:
    def __init__(self):
        self.previous: ScreenFrame | None = None
        self.revision = 0

    def update(self, frame: ScreenFrame, now: float | None = None) -> ScreenPatch:
        previous = self.previous
        resized = previous is not None and (previous.cols, previous.rows) != (frame.cols, frame.rows)
        scrolled = previous is not None and previous.scrolls != frame.scrolls
        reset = previous is not None and previous.resets != frame.resets
        old = previous.lines if previous else ()
        changes = []
        for row in range(max(len(old), len(frame.lines))):
            before = old[row] if row < len(old) else ""
            after = frame.lines[row] if row < len(frame.lines) else ""
            if before == after:
                continue
            start = 0
            while start < min(len(before), len(after)) and before[start] == after[start]:
                start += 1
            end = max(len(before), len(after))
            # end includes erased trailing cells; consumers can clear them.
            changes.append(RowChange(row, start, end, before, after))
        full = previous is None or resized or reset or scrolled
        if changes and len(changes) >= max(8, math.ceil(len(frame.lines) * .65)):
            full = True
        if changes or full:
            self.revision += 1
        self.previous = frame
        return ScreenPatch(self.revision, tuple(changes), full,
                           time.monotonic() if now is None else now,
                           frame, scrolled, resized, reset)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def render_blocks(frame: ScreenFrame) -> list[str]:
    """Up to 16 full-screen blocks, bounded using worst-case UTF-16/fence cost.

    Remote PTYs use the same size bounds, so every visible row is represented.
    Refuse unsupported geometry rather than silently displaying a partial screen.
    """
    if not 1 <= frame.cols <= MAX_COLS or not 1 <= frame.rows <= MAX_ROWS:
        raise ValueError("remote screen must be within 240 columns x 60 rows")
    # Two UTF-16 units per terminal cell, plus fence escaping and newline.
    row_budget = frame.cols * 3 + 1
    per_block = min(8, max(1, (DISCORD_LIMIT - 40) // row_budget))
    blocks = []
    for start in range(0, frame.rows, per_block):
        lines = frame.lines[start:start + per_block]
        safe = "\n".join(lines).replace("```", "`\u200b``")
        block = "```text\n" + (safe if safe.strip() else "(빈 화면)") + "\n```"
        if utf16_len(block) > DISCORD_LIMIT:
            raise ValueError("screen row exceeds configured geometry")
        blocks.append(block)
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("too many live blocks")
    return blocks


class BlockPublisher:
    """Debounce outside flush, coalesce while sending, retry without losing state.

    A transport implements send(index, text), edit(message, index, text), and
    delete(message). discord.py already handles route-level rate limits; escaped
    HTTP failures receive bounded exponential backoff here as well.
    """

    def __init__(self, transport, *, clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self.clock = clock
        self.metrics = SyncMetrics()
        self.messages: list = []
        self._contents: list[str] = []
        self._pending: tuple[int, list[str], float, bool] | None = None
        self._latest_revision = -1
        self.applied_revision = -1
        self._epoch = 0
        self._force = True
        self._lock = asyncio.Lock()
        self.retry_at = 0.0
        self._failures = 0

    def submit(self, patch: ScreenPatch) -> None:
        self.metrics.snapshots += 1
        self.metrics.changed_rows += len(patch.changed_rows)
        self.submit_blocks(patch.revision, render_blocks(patch.frame),
                           patch.timestamp, patch.full_refresh)

    def submit_blocks(self, revision: int, blocks: list[str], timestamp: float | None = None,
                      full_refresh: bool = False) -> None:
        if revision < self._latest_revision:
            return
        if revision == self._latest_revision and not self._force:
            self.metrics.duplicate_updates += 1
            return
        self._latest_revision = revision
        self._pending = (revision, list(blocks), self.clock() if timestamp is None else timestamp,
                         full_refresh or self._force)

    def reconnect(self) -> None:
        # Fence in-flight awaits: their returned messages cannot join a new set.
        self._epoch += 1
        self.messages = []
        self._contents = []
        self._force = True
        self.retry_at = 0

    async def flush(self) -> bool:
        async with self._lock:
            while self._pending is not None:
                if self.clock() < self.retry_at:
                    return False
                pending = self._pending
                revision, blocks, queued, full = pending
                epoch = self._epoch
                if full:
                    self.metrics.full_refreshes += 1
                self.metrics.queue_wait_seconds += max(0, self.clock() - queued)
                try:
                    for index, block in enumerate(blocks):
                        # Changes that arrived at an await supersede the old frame.
                        if epoch != self._epoch or self._pending is not pending:
                            break
                        if index < len(self.messages):
                            if block == self._contents[index]:
                                continue
                            await self.transport.edit(self.messages[index], index, block)
                            self.metrics.discord_edits += 1
                        else:
                            message = await self.transport.send(index, block)
                            self.metrics.discord_sends += 1
                            if epoch != self._epoch:
                                break
                            self.messages.append(message)
                            self._contents.append("")
                        self._contents[index] = block
                        self.metrics.changed_blocks += 1
                        self.metrics.payload_utf16_units += utf16_len(block)
                    else:
                        while len(self.messages) > len(blocks):
                            await self.transport.delete(self.messages[-1])
                            if epoch != self._epoch:
                                break
                            self.messages.pop()
                            self._contents.pop()
                        if epoch == self._epoch and self._pending is pending:
                            self.applied_revision = revision
                            self._pending = None
                            self._force = False
                            self._failures = 0
                            self.retry_at = 0
                            return True
                    if epoch != self._epoch:
                        return False
                    # An updated pending revision is processed while holding the lock.
                except asyncio.CancelledError:
                    # A cancelled send may have reached Discord; recreate on resume.
                    self.reconnect()
                    raise
                except Exception as exc:
                    self.metrics.failures += 1
                    self._failures += 1
                    if getattr(exc, "status", None) == 404:
                        self.reconnect()
                    self.retry_at = self.clock() + min(60, max(
                        float(getattr(exc, "retry_after", 0) or 0), 2 ** min(self._failures, 5)))
                    return False
            return True

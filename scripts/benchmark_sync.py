"""Reproducible synthetic comparison with the pre-diff publishing algorithm.

No Discord network calls. Counts embed description UTF-16 units, not actual
network bytes (JSON, HTTP, retries and initial block sends are separate).
Run: python scripts/benchmark_sync.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dishell.diff import BlockPublisher, ScreenDiffEngine, utf16_len
from dishell.screen import TerminalScreen, render_for_discord


class CountingTransport:
    async def send(self, index, text):
        return index

    async def edit(self, message, index, text):
        pass

    async def delete(self, message):
        pass


async def benchmark():
    screen = TerminalScreen(120, 60)
    publisher = BlockPublisher(CountingTransport())
    diff = ScreenDiffEngine()
    baseline = dict(snapshots=0, edits=0, sends=0, payload_utf16_units=0)
    last_snapshot = None
    baseline_chunks = []
    last_frame = None
    for tick in range(121):
        if tick == 0:
            screen.feed("\r\n".join(f"{row:02}: " + "x" * 110 for row in range(60)).encode())
        elif tick % 3 == 0:
            # Same output schedule, one cursor-positioned edit per third poll.
            row = 1 + (tick // 3) % 60
            screen.feed(f"\x1b[{row};6H{tick:03}".encode())
        # Baseline main: rebuild screen text every tick and edit every chunk
        # whenever the snapshot changes (even when a chunk is unchanged).
        baseline["snapshots"] += 1
        lines = [line.rstrip() for line in screen._screen.display]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        snapshot = "\n".join(lines)
        if snapshot != last_snapshot:
            chunks = render_for_discord(snapshot)
            baseline["edits"] += min(len(chunks), len(baseline_chunks))
            baseline["sends"] += max(0, len(chunks) - len(baseline_chunks))
            baseline["payload_utf16_units"] += sum(utf16_len(chunk) for chunk in chunks)
            baseline_chunks, last_snapshot = chunks, snapshot
        frame = screen.capture()
        if frame is not last_frame:
            publisher.submit(diff.update(frame))
            last_frame = frame
        else:
            publisher.metrics.duplicate_updates += 1
        await publisher.flush()
    metrics = publisher.metrics
    improved = dict(snapshots=screen.snapshot_count, edits=metrics.discord_edits,
                    sends=metrics.discord_sends, changed_rows=metrics.changed_rows,
                    changed_blocks=metrics.changed_blocks, duplicates=metrics.duplicate_updates,
                    full_refreshes=metrics.full_refreshes,
                    queue_wait_seconds=round(metrics.queue_wait_seconds, 6),
                    payload_utf16_units=metrics.payload_utf16_units)
    assert baseline["snapshots"] == 121 and improved["snapshots"] == 41
    assert baseline["edits"] == 80 and improved["edits"] == 40
    assert improved["payload_utf16_units"] < baseline["payload_utf16_units"]
    return {"scenario": "120x60 screen; 121 polls; one row changes every 3 polls",
            "baseline": baseline, "improved": improved}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(benchmark()), indent=2))

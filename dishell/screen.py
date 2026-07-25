"""A headless terminal emulator that turns agy's PTY output into a snapshot.

agy draws a full-screen TUI: it repaints, moves the cursor and overwrites lines
in place. Streaming those bytes to Discord verbatim produces unreadable noise,
so the raw output is replayed into an in-memory screen and only the resulting
picture is published.
"""

import codecs

import pyte

DEFAULT_COLS = 100
DEFAULT_ROWS = 30

# Discord's embed description cap is 4096; leave room for the code fence.
DISCORD_LIMIT = 3900


class TerminalScreen:
    """An in-memory VT screen fed with the raw bytes agy writes to the PTY."""

    def __init__(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS):
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> None:
        """Replay ``data``; partial multi-byte characters are carried over."""
        text = self._decoder.decode(data)
        if text:
            self._stream.feed(text)

    def resize(self, cols: int, rows: int) -> None:
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self._screen.resize(rows, cols)

    def snapshot(self) -> str:
        """The current screen with trailing blank space removed."""
        lines = [line.rstrip() for line in self._screen.display]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def render_for_discord(snapshot: str, limit: int = DISCORD_LIMIT) -> str:
    """Wrap ``snapshot`` in a code fence that fits in one Discord embed.

    When the screen is too tall the *tail* is kept, since that is where a TUI
    puts its most recent output and its input prompt.
    """
    if not snapshot.strip():
        return "```text\n(빈 화면)\n```"

    # A fence inside the payload would terminate our own code block early.
    safe = snapshot.replace("```", "`​``")

    fence_overhead = len("```text\n\n```")
    budget = limit - fence_overhead
    if len(safe) > budget:
        lines = safe.split("\n")
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            cost = len(line) + 1
            if used + cost > budget:
                break
            kept.insert(0, line)
            used += cost
        safe = "\n".join(kept) if kept else safe[-budget:]

    return f"```text\n{safe}\n```"

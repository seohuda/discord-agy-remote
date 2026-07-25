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
        self._screen = pyte.HistoryScreen(cols, rows, history=10000)
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def pop_history(self) -> list[str]:
        """Return and clear the scrolled-off lines from the top history."""
        lines = []
        if hasattr(self._screen, "history"):
            while self._screen.history.top:
                row = self._screen.history.top.popleft()
                lines.append("".join(c.data for c in row.values()))
        return lines

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


def render_for_discord(snapshot: str, history_lines: list[str] = None, limit: int = DISCORD_LIMIT) -> list[str]:
    """Wrap output in code fences, splitting into multiple if needed."""
    lines = []
    if history_lines:
        lines.extend([line.rstrip() for line in history_lines])
    
    lines.extend([line.rstrip() for line in snapshot.split("\n")])
    
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
        
    if not lines:
        return ["```text\n(빈 화면)\n```"]
        
    chunks = []
    current_chunk = []
    current_len = 0
    fence_overhead = len("```text\n\n```")
    budget = limit - fence_overhead
    
    for line in lines:
        safe_line = line.replace("```", "`​``")
        cost = len(safe_line) + 1
        
        if current_len + cost > budget and current_chunk:
            chunks.append("```text\n" + "\n".join(current_chunk) + "\n```")
            current_chunk = []
            current_len = 0
            
        current_chunk.append(safe_line)
        current_len += cost
        
    if current_chunk:
        chunks.append("```text\n" + "\n".join(current_chunk) + "\n```")
        
    if len(chunks) > 4:
        chunks = chunks[-4:]
        chunks[0] = "```text\n...(출력이 너무 길어 상단이 생략되었습니다)...\n" + chunks[0][8:]
        
    return chunks

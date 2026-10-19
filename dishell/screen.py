"""A headless terminal emulator that turns agy's PTY output into a snapshot.

agy draws a full-screen TUI: it repaints, moves the cursor and overwrites lines
in place. Streaming those bytes to Discord verbatim produces unreadable noise,
so the raw output is replayed into an in-memory screen and only the resulting
picture is published.
"""

import codecs
from dataclasses import dataclass

import pyte

DEFAULT_COLS = 100
DEFAULT_ROWS = 30

# Discord's embed description cap is 4096; leave room for the code fence.
DISCORD_LIMIT = 3900


@dataclass(frozen=True)
class ScreenFrame:
    """An immutable, position-preserving view of the terminal."""

    lines: tuple[str, ...]
    cols: int
    rows: int
    version: int = 0
    scrolls: int = 0
    resets: int = 0


class _TrackedScreen(pyte.HistoryScreen):
    def __init__(self, *args, **kwargs):
        self.scrolls = 0
        self.resets = 0
        super().__init__(*args, **kwargs)

    def index(self):
        bottom = self.margins.bottom if self.margins else self.lines - 1
        if self.cursor.y == bottom:
            self.scrolls += 1
        super().index()

    def reverse_index(self):
        top = self.margins.top if self.margins else 0
        if self.cursor.y == top:
            self.scrolls += 1
        super().reverse_index()

    def reset(self):
        self.resets += 1
        super().reset()

    def erase_in_display(self, how=0, *args, **kwargs):
        if how in (2, 3):
            self.resets += 1
        super().erase_in_display(how, *args, **kwargs)


class TerminalScreen:
    """An in-memory VT screen fed with the raw bytes agy writes to the PTY."""

    def __init__(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS):
        self.cols = cols
        self.rows = rows
        self._screen = _TrackedScreen(cols, rows, history=10000)
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.version = 0
        self.snapshot_count = 0
        self._cached_frame: ScreenFrame | None = None

    def pop_history(self) -> list[str]:
        """Return and clear the lines that have scrolled off the top."""
        lines = []
        while self._screen.history.top:
            row = self._screen.history.top.popleft()
            # A history row is a sparse map keyed by column, so it has to be
            # read by index: iterating its values would drop the gaps between
            # written cells and follow write order rather than column order.
            lines.append("".join(row[x].data for x in range(self.cols)).rstrip())
        return lines

    def feed(self, data: bytes) -> None:
        """Replay ``data``; partial multi-byte characters are carried over."""
        text = self._decoder.decode(data)
        if text:
            self._stream.feed(text)
            self.version += 1

    def resize(self, cols: int, rows: int) -> None:
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self._screen.resize(rows, cols)
        self.version += 1

    def capture(self) -> ScreenFrame:
        """Build once per output batch; retain blank rows and their positions."""
        if self._cached_frame is None or self._cached_frame.version != self.version:
            self.snapshot_count += 1
            self._cached_frame = ScreenFrame(
                tuple(line.rstrip() for line in self._screen.display),
                self.cols, self.rows, self.version,
                self._screen.scrolls, self._screen.resets,
            )
        return self._cached_frame

    def snapshot(self) -> str:
        """The current screen with trailing blank space removed."""
        lines = list(self.capture().lines)
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


FENCE_OPEN = "```text\n"
FENCE_CLOSE = "\n```"
TRUNCATED = "...(출력이 너무 길어 상단이 생략되었습니다)..."
MAX_CHUNKS = 4


def utf16_len(text: str) -> int:
    """Discord limits count UTF-16 code units, including astral characters."""
    return len(text.encode("utf-16-le")) // 2


def _fence(lines: list[str]) -> str:
    return FENCE_OPEN + "\n".join(lines) + FENCE_CLOSE


def render_for_discord(
    snapshot: str,
    history_lines: list[str] | None = None,
    limit: int = DISCORD_LIMIT,
) -> list[str]:
    """Wrap the screen (and any scrolled-off history above it) in code fences.

    Returns one message per fence, oldest first, each within ``limit``. Only
    the last ``MAX_CHUNKS`` are kept so a burst of output cannot turn into a
    wall of DMs.
    """
    lines = [line.rstrip() for line in (history_lines or [])]
    lines += [line.rstrip() for line in snapshot.split("\n")]

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    if not lines:
        return [_fence(["(빈 화면)"])]

    budget = limit - len(FENCE_OPEN) - len(FENCE_CLOSE)
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        # An inner fence would close ours early; a zero-width space breaks it.
        safe = line.replace("```", "`​``")
        # A single line can outgrow the budget on its own (a long pasted path,
        # say), so it is split rather than sent over the limit and rejected.
        for piece in _split(safe, budget) or [""]:
            cost = utf16_len(piece) + 1
            if current and current_len + cost > budget:
                groups.append(current)
                current, current_len = [], 0
            current.append(piece)
            current_len += cost

    if current:
        groups.append(current)

    if len(groups) > MAX_CHUNKS:
        groups = groups[-MAX_CHUNKS:]
        # The marker takes the place of the oldest surviving line so the
        # first chunk stays within the limit.
        groups[0] = [TRUNCATED, *groups[0][1:]]
        while len(groups[0]) > 1 and utf16_len("\n".join(groups[0])) > budget:
            groups[0].pop(1)
        groups[0][0] = _split(groups[0][0], budget)[0]

    return [_fence(group) for group in groups]


def _split(line: str, width: int) -> list[str]:
    groups, current, size = [], [], 0
    for char in line:
        cost = 2 if ord(char) > 0xFFFF else 1
        if current and size + cost > width:
            groups.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += cost
    if current:
        groups.append("".join(current))
    return groups

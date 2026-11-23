"""Intercept ``/rc`` commands typed in the local terminal before agy sees them.

The local terminal runs in raw mode, so there are no lines to inspect -- input
arrives one keystroke at a time and every byte is meant for agy's TUI. Bytes are
therefore held back only while they still spell the beginning of a local
command; the moment they cannot, the whole buffer is released to agy. A command
that completes is consumed entirely, so agy never sees it and there is nothing
to erase from its input box.
"""

COMMANDS = ("/rc on", "/rc off", "/rc status")

_ENTER = (0x0D, 0x0A)
_ERASE = (0x7F, 0x08)  # backspace / delete
_KILL_LINE = 0x15  # ctrl-u
_INTERRUPT = 0x03  # ctrl-c
_ESC = 0x1B
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_MAX_ESC_LEN = 16


def _count_chars(data: bytes) -> int:
    """Count UTF-8 characters (ignoring continuation bytes) in ``data``."""
    return sum(1 for b in data if (b & 0xC0) != 0x80)


class LocalCommandFilter:
    def __init__(self, commands=COMMANDS):
        self.commands = tuple(" ".join(c.lower().split()) for c in commands)
        # "/rc" -- once input starts with this root + space, it cannot be an
        # agy command, so it is held to the end of the line even if mistyped.
        self._roots = tuple({c.split()[0] for c in self.commands})
        self._held = bytearray()
        self._line_chars = 0
        self._esc_buf = bytearray()
        self._in_paste = False
        self._paste_has_data = False

    @property
    def pending(self) -> bytes:
        """Bytes currently held back from agy."""
        return bytes(self._held)

    def _is_candidate(self) -> bool:
        """Whether the held text could still be a local command.

        True while it is being spelled out (``/r``, ``/rc``) and, once the
        ``/rc `` prefix is complete, for the rest of the line -- so a typo is
        corrected in place rather than dumped into agy's input box.
        """
        text = self._held.decode("utf-8", errors="replace").lower()
        return any(
            root.startswith(text) or text == root or text.startswith(root + " ")
            for root in self._roots
        )

    def _pop_char(self) -> None:
        """Remove one UTF-8 character from the held buffer."""
        while self._held:
            removed = self._held.pop()
            if (removed & 0xC0) != 0x80:
                break

    def _track_forwarded(self, byte: int) -> None:
        """Observe a forwarded byte to track line position and bracketed paste."""
        if self._esc_buf:
            if len(self._esc_buf) == 1 and byte not in (ord("["), ord("O")):
                # Standalone ESC followed by a non-CSI/SS3 byte; stop tracking
                # the escape and track this byte normally below.
                self._esc_buf.clear()
            else:
                self._esc_buf.append(byte)
                if len(self._esc_buf) >= 3 and (
                    0x40 <= byte <= 0x7E or len(self._esc_buf) >= _MAX_ESC_LEN
                ):
                    seq = bytes(self._esc_buf)
                    self._esc_buf.clear()
                    if seq == _PASTE_START:
                        self._in_paste = True
                        self._paste_has_data = False
                    elif seq == _PASTE_END:
                        self._in_paste = False
                        if self._paste_has_data:
                            self._line_chars = max(self._line_chars, 1)
                return

        if byte == _ESC:
            self._esc_buf.append(byte)
            return

        if self._in_paste:
            if byte >= 0x20:
                self._paste_has_data = True
            return

        if byte in _ENTER or byte in (_KILL_LINE, _INTERRUPT):
            self._line_chars = 0
        elif byte in _ERASE:
            self._line_chars = max(self._line_chars - 1, 0)
        elif byte >= 0x20 and (byte & 0xC0) != 0x80:
            self._line_chars += 1

    def feed(self, data: bytes) -> tuple[bytes, list[str]]:
        """Split ``data`` into bytes for agy and completed local commands."""
        forward = bytearray()
        commands: list[str] = []

        for byte in data:
            if not self._held:
                if self._esc_buf == b"\x1b" and byte not in (ord("["), ord("O")):
                    self._esc_buf.clear()

                if (
                    byte == 0x2F
                    and self._line_chars == 0
                    and not self._in_paste
                    and not self._esc_buf
                ):
                    self._held.append(byte)
                else:
                    forward.append(byte)
                    self._track_forwarded(byte)
                continue

            # Held bytes never reached agy, so editing them must not either --
            # forwarding an erase here would delete agy's own input instead.
            if byte in _ERASE:
                self._pop_char()
                continue

            if byte == _KILL_LINE:
                self._held.clear()
                continue

            if byte == _INTERRUPT:
                self._held.clear()
                forward.append(byte)
                self._track_forwarded(byte)
                continue

            if byte in _ENTER:
                text = " ".join(
                    self._held.decode("utf-8", errors="replace").lower().split()
                )
                if text in self.commands:
                    commands.append(text)
                else:
                    forward += self._held
                    forward.append(byte)
                self._held.clear()
                self._line_chars = 0
                continue

            # Never hold control bytes (ESC, Tab, arrow-key prefixes, etc.):
            # echoing them in the status bar would inject raw ANSI sequences,
            # and keys like Tab are likely meant for agy's own slash menu.
            if byte < 0x20:
                self._line_chars += _count_chars(self._held)
                forward += self._held
                self._held.clear()
                forward.append(byte)
                self._track_forwarded(byte)
                continue

            self._held.append(byte)
            if not self._is_candidate():
                self._line_chars += _count_chars(self._held)
                forward += self._held
                self._held.clear()

        return bytes(forward), commands

    def flush(self) -> bytes:
        """Release anything still held, e.g. when the session ends."""
        held = bytes(self._held)
        self._held.clear()
        return held

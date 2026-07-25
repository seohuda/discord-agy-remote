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


class LocalCommandFilter:
    def __init__(self, commands=COMMANDS):
        self.commands = tuple(c.lower() for c in commands)
        self._held = bytearray()

    @property
    def pending(self) -> bytes:
        """Bytes currently held back from agy."""
        return bytes(self._held)

    def _is_prefix(self) -> bool:
        # Stripped, so trailing spaces after a complete command stay held.
        text = self._held.decode("utf-8", errors="replace").strip().lower()
        return any(c.startswith(text) for c in self.commands)

    def feed(self, data: bytes) -> tuple[bytes, list[str]]:
        """Split ``data`` into bytes for agy and completed local commands."""
        forward = bytearray()
        commands: list[str] = []

        for byte in data:
            if not self._held:
                if byte == 0x2F:  # "/" may start a local command
                    self._held.append(byte)
                else:
                    forward.append(byte)
                continue

            if byte in _ENTER:
                text = self._held.decode("utf-8", errors="replace").strip().lower()
                if text in self.commands:
                    commands.append(text)
                else:
                    forward += self._held
                    forward.append(byte)
                self._held.clear()
                continue

            self._held.append(byte)
            if not self._is_prefix():
                forward += self._held
                self._held.clear()

        return bytes(forward), commands

    def flush(self) -> bytes:
        """Release anything still held, e.g. when the session ends."""
        held = bytes(self._held)
        self._held.clear()
        return held

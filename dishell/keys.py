"""Named key sequences that can be injected into the agy PTY from Discord."""


class UnknownKey(ValueError):
    """Raised when a key name has no known escape sequence."""


KEY_SEQUENCES: dict[str, bytes] = {
    "enter": b"\r",
    "esc": b"\x1b",
    "escape": b"\x1b",
    "tab": b"\t",
    "shift-tab": b"\x1b[Z",
    "space": b" ",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "ctrl-a": b"\x01",
    "ctrl-b": b"\x02",
    "ctrl-c": b"\x03",
    "ctrl-d": b"\x04",
    "ctrl-e": b"\x05",
    "ctrl-l": b"\x0c",
    "ctrl-n": b"\x0e",
    "ctrl-p": b"\x10",
    "ctrl-r": b"\x12",
    "ctrl-u": b"\x15",
    "ctrl-w": b"\x17",
    "y": b"y",
    "n": b"n",
}


def key_names() -> list[str]:
    """Key names accepted by :func:`resolve_keys`, in a stable order."""
    return sorted(KEY_SEQUENCES)


def resolve_keys(names) -> bytes:
    """Concatenate the escape sequences for ``names``.

    Names are case-insensitive and tolerate ``ctrl+c`` as well as ``ctrl-c``.
    """
    out = bytearray()
    for name in names:
        key = name.strip().lower().replace("+", "-")
        if key not in KEY_SEQUENCES:
            raise UnknownKey(name)
        out += KEY_SEQUENCES[key]
    return bytes(out)

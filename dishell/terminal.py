"""PTY plumbing: run agy on a real terminal and mirror the local one into it.

agy only starts its interactive TUI when stdin/stdout are a TTY. Plain pipes make
it fall back to one-shot print mode, so the child is given a pseudo-terminal and
the user's own terminal is put in raw mode and forwarded byte for byte.
"""

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import tty


class PtyProcess:
    """A child process attached to a pseudo-terminal."""

    def __init__(self, argv: list[str]):
        self.argv = argv
        self.pid: int | None = None
        self.master_fd: int | None = None
        self.exit_status: int | None = None

    def spawn(self, cols: int, rows: int) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvp(self.argv[0], self.argv)
            except OSError as exc:
                sys.stderr.write(f"[Dishell] cannot execute {self.argv[0]!r}: {exc}\n")
                sys.stderr.flush()
            os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        os.set_blocking(master_fd, False)
        self.set_winsize(cols, rows)

    def set_winsize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    def write(self, data: bytes) -> bytes:
        """Write to the child's terminal; return whatever did not fit.

        The master fd is non-blocking, so a large paste can be accepted only
        partially. The caller is expected to retry the remainder rather than
        drop it, which would silently truncate a prompt.
        """
        if self.master_fd is None or not data:
            return b""
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.master_fd, view)
            except BlockingIOError:
                return bytes(view)
            except OSError:
                return b""
            if not written:
                return bytes(view)
            view = view[written:]
        return b""

    def read(self) -> bytes | None:
        """Read available output. ``None`` means the child's terminal closed."""
        if self.master_fd is None:
            return None
        try:
            data = os.read(self.master_fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as exc:
            # EIO is how a PTY master reports that the child has exited.
            if exc.errno in (errno.EIO, errno.EBADF):
                return None
            raise
        return data or None

    def reap(self, block: bool = True) -> int | None:
        """Collect the child's exit status once it has terminated."""
        if self.pid is None or self.exit_status is not None:
            return self.exit_status
        try:
            pid, status = os.waitpid(self.pid, 0 if block else os.WNOHANG)
        except ChildProcessError:
            self.exit_status = 0
            return self.exit_status
        if pid == 0:
            return None
        if os.WIFEXITED(status):
            self.exit_status = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.exit_status = 128 + os.WTERMSIG(status)
        else:
            self.exit_status = 0
        return self.exit_status

    def terminate(self) -> None:
        if self.pid is not None and self.exit_status is None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass

    def close(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


class LocalTerminal:
    """The user's own terminal: raw mode, size, and resize notifications."""

    def __init__(self, fd: int | None = None):
        self.fd = fd if fd is not None else sys.stdin.fileno()
        self.is_tty = os.isatty(self.fd)
        self._saved: list | None = None

    def size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(self.fd)
            if size.columns and size.lines:
                return size.columns, size.lines
        except OSError:
            pass
        return 100, 30

    def enter_raw(self) -> None:
        """Stop the tty from echoing and line-buffering, so agy sees every key."""
        if not self.is_tty:
            return
        self._saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)

    def restore(self) -> None:
        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def write(self, data: bytes) -> None:
        try:
            os.write(sys.stdout.fileno(), data)
        except OSError:
            pass

    def notice(self, text: str) -> None:
        """Print a wrapper message on its own line without the tty's echo."""
        self.write(b"\r\n" + text.encode("utf-8") + b"\r\n")

    def on_resize(self, loop: asyncio.AbstractEventLoop, callback) -> None:
        try:
            loop.add_signal_handler(signal.SIGWINCH, callback)
        except (NotImplementedError, ValueError):
            pass

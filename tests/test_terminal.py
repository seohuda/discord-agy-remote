import select
import sys
import time

from dishell.terminal import PtyProcess


def drain(proc: PtyProcess, timeout: float = 10.0) -> bytes:
    """Read until the child's PTY closes."""
    out = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.master_fd], [], [], 0.2)
        if not ready:
            continue
        chunk = proc.read()
        if chunk is None:
            break
        out += chunk
    return bytes(out)


def run(code: str, cols: int = 80, rows: int = 24) -> tuple[bytes, int]:
    proc = PtyProcess([sys.executable, "-c", code])
    proc.spawn(cols, rows)
    try:
        output = drain(proc)
        return output, proc.reap()
    finally:
        proc.close()


def test_child_stdin_and_stdout_are_a_tty():
    output, status = run("import sys; print(sys.stdin.isatty(), sys.stdout.isatty())")
    assert b"True True" in output
    assert status == 0


def test_child_sees_the_requested_window_size():
    output, _ = run("import os; print(tuple(os.get_terminal_size()))", cols=120, rows=40)
    assert b"(120, 40)" in output


def test_exit_status_is_propagated():
    _, status = run("import sys; sys.exit(3)")
    assert status == 3


def test_signal_death_is_reported_as_128_plus_signal():
    _, status = run("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    assert status == 128 + 9


def test_input_written_to_the_pty_reaches_the_child():
    proc = PtyProcess([sys.executable, "-c", "print('got', input())"])
    proc.spawn(80, 24)
    try:
        time.sleep(0.3)
        assert proc.write(b"ping\r") == b""  # nothing left over
        output = drain(proc)
        assert b"got ping" in output
        assert proc.reap() == 0
    finally:
        proc.close()


def test_resize_is_visible_to_the_child():
    code = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGWINCH, lambda *a: (print(tuple(os.get_terminal_size())), sys.stdout.flush()))\n"
        "print('ready'); sys.stdout.flush()\n"
        "signal.pause()\n"
    )
    proc = PtyProcess([sys.executable, "-c", code])
    proc.spawn(80, 24)
    try:
        time.sleep(0.5)
        proc.set_winsize(100, 30)
        deadline = time.monotonic() + 5
        seen = bytearray()
        while time.monotonic() < deadline and b"(100, 30)" not in seen:
            chunk = proc.read()
            if chunk is None:
                break
            seen += chunk
            time.sleep(0.05)
        assert b"(100, 30)" in seen
    finally:
        proc.terminate()
        proc.reap()
        proc.close()


def test_a_large_paste_is_never_silently_truncated():
    """The master fd is non-blocking; anything it refuses must come back."""
    payload = ("x" * 200 + "\r").encode()
    proc = PtyProcess([sys.executable, "-c", "print('len', len(input()))"])
    proc.spawn(80, 24)
    try:
        time.sleep(0.3)
        pending = proc.write(payload)
        deadline = time.monotonic() + 5
        while pending and time.monotonic() < deadline:
            pending = proc.write(pending)
        assert pending == b""
        assert b"len 200" in drain(proc)
    finally:
        proc.close()


def test_write_to_a_closed_terminal_reports_nothing_pending():
    proc = PtyProcess([sys.executable, "-c", "pass"])
    proc.spawn(80, 24)
    drain(proc)
    proc.reap()
    proc.close()
    assert proc.write(b"anything") == b""


def test_missing_executable_exits_with_127():
    proc = PtyProcess(["definitely-not-a-real-binary-xyz"])
    proc.spawn(80, 24)
    try:
        drain(proc)
        assert proc.reap() == 127
    finally:
        proc.close()

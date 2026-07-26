from dishell.screen import TerminalScreen, render_for_discord


def test_plain_output_appears_in_snapshot():
    screen = TerminalScreen(40, 10)
    screen.feed(b"hello world\r\n")
    assert screen.snapshot() == "hello world"


def test_carriage_return_overwrite_keeps_only_the_final_state():
    screen = TerminalScreen(40, 10)
    for frame in (b"| working", b"\r/ working", b"\r- done    "):
        screen.feed(frame)
    assert screen.snapshot() == "- done"


def test_cursor_positioning_repaints_in_place():
    screen = TerminalScreen(40, 10)
    screen.feed(b"first\r\nsecond\r\n")
    screen.feed(b"\x1b[1;1H")  # home
    screen.feed(b"FIRST")
    assert screen.snapshot() == "FIRST\nsecond"


def test_colour_codes_do_not_leak_into_the_snapshot():
    screen = TerminalScreen(40, 10)
    screen.feed(b"\x1b[31mred\x1b[0m")
    assert screen.snapshot() == "red"


def test_blank_padding_is_trimmed():
    screen = TerminalScreen(40, 10)
    screen.feed(b"\r\n\r\n  text  \r\n\r\n")
    assert screen.snapshot() == "  text"


def test_multibyte_split_across_reads_is_not_corrupted():
    screen = TerminalScreen(40, 10)
    payload = "한글".encode("utf-8")
    screen.feed(payload[:3])
    screen.feed(payload[3:])
    assert screen.snapshot() == "한글"


def test_resize_changes_the_wrapping_width():
    screen = TerminalScreen(10, 5)
    screen.resize(40, 5)
    assert screen.cols == 40
    screen.feed(b"x" * 30)
    assert screen.snapshot() == "x" * 30


def test_render_wraps_in_a_code_fence():
    assert render_for_discord("hi") == ["```text\nhi\n```"]


def test_render_neutralises_inner_fences():
    rendered = render_for_discord("```py\ncode\n```")[0]
    assert rendered.count("```") == 2
    assert "code" in rendered


def test_render_keeps_the_tail_and_respects_the_limit():
    snapshot = "\n".join(f"line {i}" for i in range(500))
    rendered = render_for_discord(snapshot, limit=200)
    assert len(rendered) <= 4
    assert "line 499" in rendered[-1]
    assert "line 0\n" not in rendered[0]


def test_render_handles_an_empty_screen():
    assert "빈 화면" in render_for_discord("   \n  ")[0]


def test_render_never_exceeds_the_limit_even_with_one_huge_line():
    """A single line longer than the budget must be broken up, not sent whole.

    Discord rejects an over-length embed outright, which would lose the frame.
    """
    rendered = render_for_discord("x" * 900, limit=200)
    assert all(len(chunk) <= 200 for chunk in rendered)
    assert "x" in rendered[-1]


def test_render_puts_history_above_the_current_screen():
    rendered = render_for_discord("now", ["older"], limit=200)
    assert rendered[0].index("older") < rendered[0].index("now")


def test_scrolled_off_lines_keep_their_column_order():
    """History rows are sparse maps keyed by column, not ordered sequences."""
    screen = TerminalScreen(20, 3)
    # Paint a later column first, then an earlier one, and scroll the line away.
    screen.feed(b"\x1b[1;10Hlate\x1b[1;1Hearly\r\n")
    for _ in range(5):
        screen.feed(b"filler\r\n")
    history = screen.pop_history()
    assert any(line == "early    late" for line in history)


def test_popping_history_twice_does_not_repeat_lines():
    screen = TerminalScreen(20, 3)
    for i in range(10):
        screen.feed(f"line {i}\r\n".encode())
    assert screen.pop_history()
    assert screen.pop_history() == []

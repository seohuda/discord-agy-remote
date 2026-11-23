from dishell.localcmd import LocalCommandFilter


def feed_all(text: str):
    """Feed ``text`` one byte at a time, as raw-mode keystrokes arrive."""
    filt = LocalCommandFilter()
    forwarded = bytearray()
    commands = []
    for byte in text.encode("utf-8"):
        out, cmds = filt.feed(bytes([byte]))
        forwarded += out
        commands += cmds
    return bytes(forwarded), commands, filt


def test_ordinary_typing_is_forwarded_untouched():
    forwarded, commands, filt = feed_all("hello\r")
    assert forwarded == b"hello\r"
    assert commands == []
    assert filt.pending == b""


def test_rc_command_is_consumed_and_never_reaches_agy():
    forwarded, commands, _ = feed_all("/rc on\r")
    assert forwarded == b""
    assert commands == ["/rc on"]


def test_all_three_commands_are_recognised():
    for text, expected in (
        ("/rc on\r", "/rc on"),
        ("/rc off\r", "/rc off"),
        ("/rc status\r", "/rc status"),
    ):
        _, commands, _ = feed_all(text)
        assert commands == [expected]


def test_commands_are_case_insensitive_and_ignore_surrounding_space():
    _, commands, _ = feed_all("/RC Off  \r")
    assert commands == ["/rc off"]


def test_agy_slash_command_is_released_as_soon_as_it_diverges():
    forwarded, commands, filt = feed_all("/model\r")
    assert forwarded == b"/model\r"
    assert commands == []
    assert filt.pending == b""


def test_divergence_releases_the_held_bytes_before_the_newline():
    filt = LocalCommandFilter()
    out, _ = filt.feed(b"/r")
    assert out == b""  # still a prefix of "/rc ..."
    out, _ = filt.feed(b"e")
    assert out == b"/re"  # "/re" cannot be a command, release immediately


def test_backspace_edits_the_held_buffer_instead_of_dumping_it():
    """The held text never reached agy, so erasing it must not reach agy."""
    forwarded, commands, filt = feed_all("/rc on\x7f\x7f")
    assert forwarded == b""
    assert commands == []
    assert filt.pending == b"/rc "


def test_a_command_retyped_after_a_correction_still_fires():
    forwarded, commands, _ = feed_all("/rc onn\x7f\r")
    assert forwarded == b""
    assert commands == ["/rc on"]


def test_erasing_past_the_slash_leaves_nothing_behind():
    forwarded, commands, filt = feed_all("/rc\x7f\x7f\x7f")
    assert forwarded == b""
    assert commands == []
    assert filt.pending == b""


def test_backspace_with_nothing_held_reaches_agy_normally():
    forwarded, _, _ = feed_all("ab\x7f")
    assert forwarded == b"ab\x7f"


def test_ctrl_u_abandons_a_half_typed_command():
    forwarded, commands, filt = feed_all("/rc o\x15")
    assert forwarded == b""
    assert commands == []
    assert filt.pending == b""


def test_ctrl_c_abandons_the_command_but_still_interrupts_agy():
    forwarded, commands, filt = feed_all("/rc o\x03")
    assert forwarded == b"\x03"
    assert commands == []
    assert filt.pending == b""


def test_partial_command_without_enter_is_not_executed():
    _, commands, filt = feed_all("/rc of")
    assert commands == []
    assert filt.pending == b"/rc of"
    assert filt.flush() == b"/rc of"
    assert filt.pending == b""


def test_unknown_rc_subcommand_falls_through_to_agy():
    forwarded, commands, _ = feed_all("/rc onx\r")
    assert commands == []
    assert forwarded == b"/rc onx\r"


def test_command_mixed_into_a_chunk_with_other_input():
    filt = LocalCommandFilter()
    forwarded, commands = filt.feed(b"hi\r/rc on\rbye\r")
    assert forwarded == b"hi\rbye\r"
    assert commands == ["/rc on"]


def test_newline_terminator_works_as_well_as_carriage_return():
    _, commands, _ = feed_all("/rc on\n")
    assert commands == ["/rc on"]


def test_slash_mid_sentence_and_paths_are_never_held_or_executed():
    filt = LocalCommandFilter()
    out, cmds = filt.feed(b"src/rc/foo.py")
    assert out == b"src/rc/foo.py"
    assert cmds == []
    assert filt.pending == b""

    forwarded, commands, _ = feed_all("how does /rc on\r")
    assert forwarded == b"how does /rc on\r"
    assert commands == []


def test_rc_without_space_releases_immediately_for_other_words():
    filt = LocalCommandFilter()
    out, _ = filt.feed(b"/rclone")
    assert out == b"/rclone"
    assert filt.pending == b""


def test_bracketed_paste_never_triggers_local_commands():
    pasted = b"\x1b[200~README:\n/rc on\nmore\x1b[201~\r"
    filt = LocalCommandFilter()
    forwarded, commands = filt.feed(pasted)
    assert forwarded == pasted
    assert commands == []

    # After submitting the paste, a fresh local command works again.
    out, commands = filt.feed(b"/rc on\r")
    assert out == b""
    assert commands == ["/rc on"]


def test_control_and_escape_keys_never_enter_held_buffer():
    filt = LocalCommandFilter()
    out, _ = filt.feed(b"/rc ")
    assert out == b""
    out, _ = filt.feed(b"\x1b[D")
    assert out == b"/rc \x1b[D"
    assert filt.pending == b""


def test_multibyte_backspace_erases_whole_character():
    forwarded, commands, filt = feed_all("/rc 한글\x7f")
    assert forwarded == b""
    assert commands == []
    assert filt.pending == "/rc 한".encode("utf-8")


def test_command_works_after_backspacing_to_empty_line_or_pressing_esc():
    forwarded, commands, _ = feed_all("ab\x7f\x7f/rc on\r")
    assert forwarded == b"ab\x7f\x7f"
    assert commands == ["/rc on"]

    forwarded, commands, _ = feed_all("\x1b/rc on\r")
    assert forwarded == b"\x1b"
    assert commands == ["/rc on"]

    forwarded, commands, _ = feed_all("\x1b[B/rc status\r")
    assert forwarded == b"\x1b[B"
    assert commands == ["/rc status"]


def test_extra_internal_spaces_are_accepted():
    forwarded, commands, _ = feed_all("/rc   on\r")
    assert forwarded == b""
    assert commands == ["/rc on"]

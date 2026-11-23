from dishell.commands import parse_remote_message


def test_plain_text_is_typed_and_submitted():
    action = parse_remote_message("파일 정리해줘")
    assert action.kind == "text"
    assert action.payload == "파일 정리해줘".encode("utf-8") + b"\r"


def test_multiline_text_uses_bracketed_paste_so_it_submits_once():
    action = parse_remote_message("line one\nline two")
    assert action.kind == "text"
    assert action.payload == b"\x1b[200~line one\nline two\x1b[201~\r"
    assert action.payload.count(b"\r") == 1


def test_agy_own_slash_commands_are_forwarded_untouched():
    action = parse_remote_message("/model")
    assert action.kind == "text"
    assert action.payload == b"/model\r"


def test_double_slash_escapes_one_slash():
    action = parse_remote_message("//rc off")
    assert action.kind == "text"
    assert action.payload == b"/rc off\r"


def test_rc_off_and_status():
    assert parse_remote_message("/rc off").kind == "rc_off"
    assert parse_remote_message("/RC Off").kind == "rc_off"
    assert parse_remote_message("/rc status").kind == "rc_status"


def test_rc_on_is_rejected_with_explanation():
    action = parse_remote_message("/rc on")
    assert action.kind == "error"
    assert "이미" in action.message


def test_rc_without_subcommand_explains_usage():
    assert parse_remote_message("/rc").kind == "error"


def test_key_single_and_multiple():
    assert parse_remote_message("/key esc").payload == b"\x1b"
    action = parse_remote_message("/key down down enter")
    assert action.kind == "keys"
    assert action.payload == b"\x1b[B\x1b[B\r"


def test_unknown_key_reports_the_offending_name():
    action = parse_remote_message("/key nope")
    assert action.kind == "error"
    assert "nope" in action.message


def test_key_without_arguments_lists_usage():
    action = parse_remote_message("/key")
    assert action.kind == "error"
    assert "/key" in action.message


def test_help():
    assert parse_remote_message("/help").kind == "help"


def test_blank_message_is_ignored():
    assert parse_remote_message("   \n ").kind == "error"


def test_say_plain_text_forwards_verbatim_including_leading_slash():
    action = parse_remote_message("/say /rc off")
    assert action.kind == "text"
    assert action.payload == b"/rc off\r"

    multiline = parse_remote_message("/say line1\nline2")
    assert multiline.kind == "text"
    assert multiline.payload == b"\x1b[200~line1\nline2\x1b[201~\r"


def test_say_without_text_returns_usage_error():
    action = parse_remote_message("/say   ")
    assert action.kind == "error"
    assert "/say" in action.message


def test_commands_accept_newline_or_tab_after_command_name():
    assert parse_remote_message("/key\nesc").payload == b"\x1b"
    assert parse_remote_message("/rc\toff").kind == "rc_off"

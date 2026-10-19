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


def test_sessions_commands_and_literal_escape_are_unambiguous():
    assert parse_remote_message("/sessions").kind == "sessions"
    assert parse_remote_message("/session start codex").arguments == ("codex",)
    assert parse_remote_message("/session attach codex-1").kind == "session_attach"
    assert parse_remote_message("/session status codex-1").kind == "session_status"
    assert parse_remote_message("/session stop codex-1").kind == "session_stop"
    assert parse_remote_message("/session history codex-1 2").arguments == ("codex-1", "2")
    assert parse_remote_message("//session start codex").payload == b"/session start codex\r"
    assert parse_remote_message("/say /session start codex").payload == b"/session start codex\r"


def test_discord_cannot_register_executables_or_pass_launch_options():
    for text in ("/session start /bin/sh", "/session start codex --dangerously-bypass-approvals-and-sandbox",
                 "/session start codex;sh", "/session stop id extra", "/session history id -1"):
        assert parse_remote_message(text).kind == "error"

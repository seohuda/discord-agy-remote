import pytest

from dishell.keys import UnknownKey, key_names, resolve_keys


def test_single_key():
    assert resolve_keys(["esc"]) == b"\x1b"


def test_multiple_keys_are_concatenated_in_order():
    assert resolve_keys(["down", "down", "enter"]) == b"\x1b[B\x1b[B\r"


def test_names_are_case_insensitive_and_accept_plus():
    assert resolve_keys(["CTRL+C"]) == resolve_keys(["ctrl-c"]) == b"\x03"


def test_unknown_key_raises():
    with pytest.raises(UnknownKey):
        resolve_keys(["down", "meta-x"])


def test_key_names_are_sorted_and_include_common_keys():
    names = key_names()
    assert names == sorted(names)
    for expected in ("ctrl-c", "enter", "esc", "tab", "up"):
        assert expected in names

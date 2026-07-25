import os
import stat

import pytest

from dishell.launch import LaunchError, resolve_agy


def make_executable(path, body="#!/bin/sh\nexit 0\n"):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_resolves_to_an_absolute_path(tmp_path, monkeypatch):
    binary = make_executable(tmp_path / "agy")
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = resolve_agy("agy", str(tmp_path / "wrapper.py"))
    assert resolved == str(binary)
    assert os.path.isabs(resolved)


def test_missing_binary_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(LaunchError) as exc:
        resolve_agy("agy", str(tmp_path / "wrapper.py"))
    assert "PATH" in str(exc.value)


def test_a_shim_pointing_back_at_the_wrapper_is_refused(tmp_path, monkeypatch):
    """Installing the wrapper on PATH would otherwise recurse forever."""
    wrapper = make_executable(tmp_path / "main.py", "#!/usr/bin/env python3\n")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    os.symlink(wrapper, shim_dir / "agy")
    monkeypatch.setenv("PATH", str(shim_dir))
    with pytest.raises(LaunchError) as exc:
        resolve_agy("agy", str(wrapper))
    assert "재귀" in str(exc.value)


def test_an_unrelated_binary_of_the_same_name_is_fine(tmp_path, monkeypatch):
    binary = make_executable(tmp_path / "agy")
    monkeypatch.setenv("PATH", str(tmp_path))
    # A different file that merely lives next to the wrapper must not trip it.
    assert resolve_agy("agy", str(tmp_path / "main.py")) == str(binary)

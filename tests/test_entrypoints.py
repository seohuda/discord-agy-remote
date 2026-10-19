"""Invoke both entry points with an actual tiny CLI, no Discord or AI credentials."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


@pytest.mark.parametrize("entry,variable,cli_args", [
    (["main.py"], "DISHELL_AGY_BIN", ["--model", "literal; harmless"]),
    (["-m", "dishell", "--cli", "codex", "--"], "DISHELL_CODEX_BIN", ["--help"]),
])
def test_legacy_agy_and_unified_entrypoint_keep_tty_arguments_and_exit_status(tmp_path, entry, variable, cli_args):
    executable = tmp_path / "mock-cli"
    executable.write_text("#!/usr/bin/env python3\nimport sys\nprint('TTY',sys.stdin.isatty(),sys.stdout.isatty(),flush=True)\nprint(repr(sys.argv[1:]),flush=True)\nsys.exit(3)\n")
    executable.chmod(0o700)
    env = dict(os.environ, DISCORD_TOKEN="", DISCORD_USER_ID="", DISHELL_AUDIT_PATH=str(tmp_path / "audit" / "audit.jsonl"))
    env[variable] = str(executable)
    result = subprocess.run([PYTHON, *entry, *cli_args], cwd=ROOT, env=env, capture_output=True, timeout=10)
    assert result.returncode == 3, result.stderr.decode()
    assert b"TTY True True" in result.stdout
    assert repr(cli_args).encode() in result.stdout

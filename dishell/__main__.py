"""Optional unified entry point; main.py and the existing agy alias stay valid."""

import argparse

from main import run_cli


def main():
    parser = argparse.ArgumentParser(description="Discord remote CLI terminal (owner DM only)")
    parser.add_argument("--cli", choices=("agy", "codex", "claude", "opencode"), default="agy")
    parser.add_argument("--remote", action="store_true", help="connect Discord on startup (read-only)")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="local CLI arguments after --")
    options = parser.parse_args()
    args = options.args[1:] if options.args[:1] == ["--"] else options.args
    return run_cli(options.cli, args, auto_remote=options.remote)


if __name__ == "__main__":
    raise SystemExit(main())

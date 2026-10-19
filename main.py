#!/usr/bin/env python3
"""Entry point: run agy under a PTY and optionally bridge it to Discord DMs."""

import asyncio
import os
import sys

from dotenv import load_dotenv

from dishell.launch import LaunchError, resolve_agy
from dishell.manager import DishellManager
from dishell.plugins import default_plugins

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    return run_cli("agy", sys.argv[1:])


def run_cli(plugin_id: str, args: list[str], *, auto_remote=False) -> int:

    load_dotenv(os.path.join(HERE, ".env"))

    try:
        plugins = default_plugins()
        argv = plugins[plugin_id].build_argv(args)
        if plugin_id == "agy":
            argv[0] = resolve_agy(plugins["agy"].executable, os.path.abspath(__file__))
    except LaunchError as exc:
        sys.stderr.write(f"[Dishell] {exc}\n")
        return 127

    token = os.getenv("DISCORD_TOKEN", "").strip()
    user_id_raw = os.getenv("DISCORD_USER_ID", "").strip()
    user_id = int(user_id_raw) if user_id_raw.isdigit() else 0

    if token and user_id:
        # Proof the wrapper is in the loop; without it a silent passthrough
        # looks identical to a working session until /rc on does nothing.
        sys.stderr.write("[Dishell] 원격 제어 대기 중 · 세션에서 /rc on\n")
    else:
        sys.stderr.write(
            "[Dishell] DISCORD_TOKEN / DISCORD_USER_ID 가 없어 '/rc on' 은 동작하지 않습니다.\n"
        )
    sys.stderr.flush()

    try:
        manager = DishellManager(token=token, user_id=user_id, argv=argv,
                                 plugin_id=plugin_id, plugins=plugins,
                                 local_args=args, auto_remote=auto_remote)
        return asyncio.run(manager.run())
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        # Do not echo arguments, environment values, or exception payloads.
        sys.stderr.write(f"[Dishell] 시작/실행 실패: {type(exc).__name__}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

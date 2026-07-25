#!/usr/bin/env python3
"""Entry point: run agy under a PTY and optionally bridge it to Discord DMs."""

import asyncio
import os
import sys

from dotenv import load_dotenv

from dishell.launch import LaunchError, resolve_agy
from dishell.manager import DishellManager

HERE = os.path.dirname(os.path.abspath(__file__))
AGY_BIN = os.getenv("DISHELL_AGY_BIN", "agy")


def main() -> int:
    args = sys.argv[1:]

    load_dotenv(os.path.join(HERE, ".env"))

    try:
        agy_path = resolve_agy(AGY_BIN, os.path.abspath(__file__))
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

    manager = DishellManager(token=token, user_id=user_id, argv=[agy_path, *args])
    try:
        return asyncio.run(manager.run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

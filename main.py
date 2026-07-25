#!/usr/bin/env python3
"""Entry point: run agy under a PTY and optionally bridge it to Discord DMs."""

import asyncio
import os
import sys

from dotenv import load_dotenv

from dishell.manager import DishellManager

AGY_BIN = os.getenv("DISHELL_AGY_BIN", "agy")
GUARD = "DISHELL_WRAPPED"


def main() -> int:
    args = sys.argv[1:]

    # If the wrapper somehow ends up invoking itself (a PATH shim rather than a
    # shell alias), hand straight over to the real agy instead of recursing.
    if os.environ.get(GUARD):
        os.execvp(AGY_BIN, [AGY_BIN, *args])

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    os.environ[GUARD] = "1"

    token = os.getenv("DISCORD_TOKEN", "").strip()
    user_id_raw = os.getenv("DISCORD_USER_ID", "").strip()
    user_id = int(user_id_raw) if user_id_raw.isdigit() else 0

    if not token or not user_id:
        # agy still has to work; only the remote bridge is unavailable.
        sys.stderr.write(
            "[Dishell] DISCORD_TOKEN / DISCORD_USER_ID 가 없어 '/rc on' 은 동작하지 않습니다.\n"
        )

    manager = DishellManager(token=token, user_id=user_id, argv=[AGY_BIN, *args])
    try:
        return asyncio.run(manager.run())
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        sys.stderr.write(f"[Dishell] '{AGY_BIN}' 를 찾을 수 없습니다. PATH를 확인하세요.\n")
        return 127


if __name__ == "__main__":
    sys.exit(main())

"""Locate the real agy binary to run under the PTY.

The wrapper is installed as a shell alias, so ``agy`` on PATH is the genuine
binary and there is nothing to recurse into. The only way recursion could happen
is if someone installs the wrapper as a PATH shim instead; that is detected by
comparing the resolved target against the wrapper itself, rather than by an
inherited environment variable -- an ambient variable silently disables the
wrapper for every descendant process, which is impossible to diagnose from the
outside.
"""

import os
import shutil


class LaunchError(RuntimeError):
    """The real agy executable could not be determined."""


def resolve_agy(name: str, wrapper_path: str) -> str:
    """Return the absolute path of ``name``, refusing to point back at us."""
    resolved = shutil.which(name)
    if resolved is None:
        raise LaunchError(
            f"'{name}' 를 PATH 에서 찾을 수 없습니다. Antigravity CLI 설치를 확인하세요."
        )

    resolved = os.path.abspath(resolved)
    try:
        if os.path.samefile(resolved, wrapper_path):
            raise LaunchError(
                f"'{name}' 가 이 래퍼 자신({wrapper_path})을 가리키고 있어 무한 재귀가 됩니다.\n"
                "래퍼는 PATH 에 두지 말고 셸 alias 로 등록하세요 (./install.sh)."
            )
    except OSError:
        pass  # one of the paths vanished; nothing sensible to compare

    return resolved

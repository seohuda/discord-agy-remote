"""Local allowlisted executable configuration. Discord never chooses argv/options."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..events import EventType, Signal
from ..launch import LaunchError

UNKNOWN = Signal(EventType.UNKNOWN, 0.0, "no_verified_signal")


class CLIPlugin(Protocol):
    id: str
    display_name: str

    def build_argv(self, args: list[str]) -> list[str]: ...
    def detect_event(self, snapshot: str) -> Signal: ...
    def detect_structured(self, event: dict) -> Signal: ...
    def get_capabilities(self) -> dict[str, bool]: ...
    def uses_jsonl(self, args: list[str]) -> bool: ...


@dataclass(frozen=True)
class BasePlugin:
    id: str
    display_name: str
    executable: str

    def build_argv(self, args: list[str]) -> list[str]:
        resolved = shutil.which(self.executable)
        if not resolved:
            raise LaunchError(f"{self.display_name}: '{self.executable}'를 PATH에서 찾을 수 없습니다. 로컬 설치/설정을 확인하세요.")
        path = Path(resolved).resolve()
        wrapper = Path(__file__).resolve().parents[2] / "main.py"
        if not path.is_file() or not os.access(path, os.X_OK) or path == wrapper:
            raise LaunchError(f"{self.display_name}: 실행 파일 경로가 유효하지 않습니다.")
        if any("\0" in arg for arg in args):
            raise LaunchError("NUL is not allowed in arguments")
        return [str(path), *args]

    def detect_event(self, snapshot: str) -> Signal:
        return UNKNOWN

    def detect_structured(self, event: dict) -> Signal:
        return UNKNOWN

    def uses_jsonl(self, args: list[str]) -> bool:
        return False

    def get_capabilities(self) -> dict[str, bool]:
        return {"interactive": True, "pty": True, "structured_events": self.id != "agy"}

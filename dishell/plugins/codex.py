from ..events import EventType, Signal
from .base import BasePlugin, UNKNOWN


class CodexPlugin(BasePlugin):
    def __init__(self, executable="codex"):
        super().__init__("codex", "Codex", executable)

    def uses_jsonl(self, args):
        return bool(args and args[0] == "exec" and "--json" in args)

    def detect_structured(self, event):
        kind = event.get("type")
        state = {"turn.started": EventType.RUNNING,
                 "turn.completed": EventType.COMPLETED,
                 "turn.failed": EventType.ERROR, "error": EventType.ERROR}.get(kind)
        if state is None:
            return UNKNOWN
        reason = "codex_" + kind
        error = event.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            reason += ": " + error["message"]
        return Signal(state, 1.0, reason, True)

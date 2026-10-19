from ..events import EventType, Signal
from .base import BasePlugin, UNKNOWN


class ClaudePlugin(BasePlugin):
    def __init__(self, executable="claude"):
        super().__init__("claude", "Claude Code", executable)

    def uses_jsonl(self, args):
        return any(args[i:i+2] == ["--output-format", "stream-json"] for i in range(len(args)))

    def detect_structured(self, event):
        if not isinstance(event, dict):
            return UNKNOWN
        if event.get("type") == "control_request" and isinstance(event.get("request"), dict):
            if event["request"].get("subtype") == "can_use_tool":
                return Signal(EventType.WAITING_APPROVAL, 1.0, "claude_can_use_tool", True)
        if event.get("type") == "result" and type(event.get("is_error")) is bool:
            subtype = event.get("subtype")
            terminal_reason = event.get("terminal_reason")
            if not isinstance(subtype, str) or not subtype:
                return UNKNOWN
            if terminal_reason is not None and not isinstance(terminal_reason, str):
                return UNKNOWN
            if terminal_reason in ("aborted_streaming", "aborted_tools"):
                state = EventType.INTERRUPTED
            elif event["is_error"]:
                state = EventType.ERROR
            elif subtype == "success":
                state = EventType.COMPLETED
            else:
                # An unknown result variant is not evidence of successful work.
                return UNKNOWN
            return Signal(state, 1.0, "claude_result_" + subtype, True)
        if event.get("type") == "assistant":
            return Signal(EventType.RUNNING, .95, "claude_assistant_message", True)
        return UNKNOWN

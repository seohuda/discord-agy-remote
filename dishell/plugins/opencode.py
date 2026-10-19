from ..events import EventType, Signal
from .base import BasePlugin, UNKNOWN


class OpenCodePlugin(BasePlugin):
    def __init__(self, executable="opencode"):
        super().__init__("opencode", "OpenCode", executable)

    def detect_structured(self, event):
        if event.get("type") != "session.status":
            return UNKNOWN
        properties = event.get("properties")
        if not isinstance(properties, dict) or not isinstance(properties.get("status"), dict):
            return UNKNOWN
        status = properties["status"].get("type")
        if not isinstance(status, str):
            return UNKNOWN
        state = {"busy": EventType.RUNNING, "idle": EventType.IDLE,
                 "retry": EventType.RUNNING}.get(status)
        return Signal(state, 1.0, "opencode_session_" + status, True) if state else UNKNOWN

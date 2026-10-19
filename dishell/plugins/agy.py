from ..alerts import APPROVAL_MARKERS, BUSY_MARKERS
from ..events import EventType, Signal, prose_lines
from .base import BasePlugin, UNKNOWN


class AgyPlugin(BasePlugin):
    def __init__(self, executable="agy"):
        super().__init__("agy", "Antigravity CLI", executable)

    def detect_event(self, snapshot):
        lines = prose_lines(snapshot)
        # Known literal prompts and footer, captured in the repository fixtures.
        choices = any(line.startswith((">", "❯", "↑/↓")) for line in lines)
        prompt = any(any(line.startswith(marker) for marker in APPROVAL_MARKERS) for line in lines)
        footer = any("navigate" in line and "confirm" in line for line in lines)
        if prompt and choices or footer and choices:
            return Signal(EventType.WAITING_APPROVAL, .9, "verified_agy_approval_screen")
        if any(marker == line for marker in BUSY_MARKERS for line in lines):
            return Signal(EventType.RUNNING, .9, "verified_agy_busy_footer")
        # A visible idle prompt is evidence of IDLE, never of successful work.
        if ">" in lines and any("? for shortcuts" in line for line in lines):
            return Signal(EventType.IDLE, .8, "verified_agy_idle_prompt")
        return UNKNOWN

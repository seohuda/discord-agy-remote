"""Detect the moments that are worth a Discord *notification*, not just an edit.

Editing a message does not push a notification to Discord clients, so the live
screen alone never pings a phone. Only a brand new message does. This module
watches the emulated screen and reports the two transitions actually worth
interrupting someone for: agy started waiting for approval, and agy finished
working.

The markers are the literal strings the agy binary prints.
"""

APPROVAL_MARKERS = (
    "do you want to proceed?",
    "do you trust the contents of this project?",
    "allow access to this file?",
    "allow creation of this file?",
    "accept this file edit?",
    "allow remote debugging?",
    "yes, and always allow",
    "yes, allow access",
    "yes, allow creation",
    "yes, i trust this folder",
    "no, and tell",
)

# agy renders this footer under any modal choice list. Matching it catches
# prompts whose wording is not enumerated above.
CHOICE_FOOTER_MARKERS = ("navigate", "confirm")

BUSY_MARKERS = ("esc to interrupt",)

APPROVAL = "approval"
DONE = "done"

_BUSY = "busy"
_IDLE = "idle"

DEFAULT_IDLE_AFTER = 3.0


def classify(snapshot: str) -> str:
    """Bucket a screen into ``approval``, ``busy`` or ``idle``."""
    text = snapshot.lower()
    if any(marker in text for marker in APPROVAL_MARKERS):
        return APPROVAL
    if all(marker in text for marker in CHOICE_FOOTER_MARKERS):
        return APPROVAL
    if any(marker in text for marker in BUSY_MARKERS):
        return _BUSY
    return _IDLE


class AlertDetector:
    """Edge-triggered detector over successive screen snapshots.

    ``update`` returns an alert name exactly once per transition, or ``None``.
    """

    def __init__(self, idle_after: float = DEFAULT_IDLE_AFTER):
        self.idle_after = idle_after
        self._state: str | None = None
        self._snapshot: str | None = None
        self._stable_since: float = 0.0
        self._work_pending = False

    @property
    def state(self) -> str | None:
        return self._state

    def update(self, snapshot: str, now: float) -> str | None:
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self._stable_since = now

        previous, current = self._state, classify(snapshot)
        self._state = current

        if current == APPROVAL:
            # A prompt supersedes any completion we were about to announce.
            self._work_pending = False
            return APPROVAL if previous != APPROVAL else None

        if previous == _BUSY and current == _IDLE:
            self._work_pending = True

        # Wait for the screen to settle so a repaint mid-transition is not
        # mistaken for the end of the run.
        if (
            self._work_pending
            and current == _IDLE
            and now - self._stable_since >= self.idle_after
        ):
            self._work_pending = False
            return DONE

        return None

"""Conservative, edge-triggered CLI state detection without Discord dependencies."""

import heapq
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

from .redaction import redact

RECENT_ACK_LIMIT = 1024


class EventType(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    ERROR = "ERROR"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Signal:
    event_type: EventType
    confidence: float
    reason: str
    authoritative: bool = False


@dataclass(frozen=True)
class CLIEvent:
    session_id: str
    plugin_id: str
    event_type: EventType
    timestamp: float
    confidence: float
    reason: str
    sequence: int

    @property
    def priority(self) -> int:
        return {EventType.WAITING_APPROVAL: 0, EventType.ERROR: 0,
                EventType.INTERRUPTED: 1, EventType.COMPLETED: 2}.get(self.event_type, 3)


def prose_lines(snapshot: str) -> list[str]:
    """Exclude fenced/indented code; generic 'error' text is never a signal."""
    result, fenced = [], False
    for line in snapshot.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and not line.startswith(("    ", "\t")):
            result.append(line.strip().lower())
    return result


class EventDetector:
    def __init__(self, session_id: str, plugin, stable_after: float = .4):
        self.session_id = session_id
        self.plugin = plugin
        self.stable_after = stable_after
        self.state = EventType.UNKNOWN
        self._candidate = None
        self._candidate_since = 0.0
        self._last_time = -1.0
        self._last_revision = -1
        self._sequence = 0
        self._exited = False
        self._structured: Signal | None = None

    def update(self, snapshot: str, *, now: float | None = None,
               revision: int = 0, structured: dict | None = None,
               exit_code: int | None = None) -> CLIEvent | None:
        now = time.monotonic() if now is None else now
        if now < self._last_time or revision < self._last_revision or self._exited:
            return None
        self._last_time, self._last_revision = now, revision
        if exit_code is not None:
            # Process death is final, even if a stale structured prompt remains.
            kind = (EventType.COMPLETED if exit_code == 0 else
                    EventType.INTERRUPTED if exit_code in (130, 143, -2, -15) else EventType.ERROR)
            signal = Signal(kind, 1.0, f"process_exit_code={exit_code}", True)
            self._exited = True
        else:
            if structured is not None:
                candidate = self.plugin.detect_structured(structured)
                if candidate.event_type != EventType.UNKNOWN:
                    self._structured = candidate
            signal = self._structured or self.plugin.detect_event(snapshot)
        if signal.event_type != self._candidate:
            self._candidate = signal.event_type
            self._candidate_since = now
        if not signal.authoritative and now - self._candidate_since < self.stable_after:
            return None
        if signal.event_type == self.state:
            return None
        self.state = signal.event_type
        self._sequence += 1
        return CLIEvent(self.session_id, self.plugin.id, self.state, time.time(),
                        signal.confidence, redact(signal.reason)[:500], self._sequence)


class EventOutbox:
    """Keep failed notifications until acknowledgement, with bounded replay history.

    Invariants (all mutations run on the owning asyncio loop):
    * ``_keys`` is exactly the unique (session_id, sequence) keys in ``_heap``.
    * ``_recent`` contains only acknowledged keys, disjoint from ``_keys``.
    * ``_recent`` holds at most RECENT_ACK_LIMIT keys, in LRU order. Very late
      replays outside this finite window may be enqueued again.
    Pending notifications are never evicted to enforce the recent-history bound.
    """

    def __init__(self):
        self._heap = []
        self._keys = set()
        self._recent = OrderedDict()
        self._counter = 0

    def put(self, event: CLIEvent) -> None:
        key = (event.session_id, event.sequence)
        if key in self._keys:
            return
        if key in self._recent:
            self._recent.move_to_end(key)
            return
        self._keys.add(key)
        self._counter += 1
        heapq.heappush(self._heap, (event.priority, self._counter, event))

    def peek(self) -> CLIEvent | None:
        return self._heap[0][2] if self._heap else None

    def acknowledge(self, event: CLIEvent) -> None:
        # Only remove the item actually sent, even if priority changed at await.
        for index, entry in enumerate(self._heap):
            if entry[2] is event:
                self._heap.pop(index)
                heapq.heapify(self._heap)
                key = (event.session_id, event.sequence)
                self._keys.remove(key)
                self._recent[key] = None
                if len(self._recent) > RECENT_ACK_LIMIT:
                    self._recent.popitem(last=False)
                return

    def __len__(self):
        return len(self._heap)

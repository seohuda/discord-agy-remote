import json
from pathlib import Path

import pytest

from dishell.events import EventDetector, EventOutbox, EventType as E
from dishell.plugins import default_plugins
from test_alerts import REAL_IDLE, REAL_TRUST_PROMPT, WORKING

FIXTURES = Path(__file__).parent / "fixtures" / "events"


@pytest.mark.parametrize("plugin_id,expected", [
    ("codex", [E.RUNNING, E.ERROR, E.COMPLETED]),
    ("claude", [E.RUNNING, E.WAITING_APPROVAL, E.COMPLETED]),
    ("opencode", [E.RUNNING, None, E.IDLE]),
])
def test_verified_structured_schema_fixtures(plugin_id, expected):
    detector = EventDetector("test-01", default_plugins()[plugin_id])
    events = [json.loads(line) for line in (FIXTURES / f"{plugin_id}.jsonl").read_text().splitlines()]
    for index, (event, state) in enumerate(zip(events, expected)):
        result = detector.update("", now=index, revision=index, structured=event)
        assert (result.event_type if result else None) == state
        if result:
            assert "private-test-key" not in result.reason
            assert result.session_id == "test-01"


def test_agy_prompt_stabilizes_once_and_idle_does_not_mean_completed():
    d = EventDetector("agy-1", default_plugins()["agy"], stable_after=.5)
    assert d.update(REAL_TRUST_PROMPT, now=0) is None
    assert d.update(REAL_TRUST_PROMPT, now=1).event_type == E.WAITING_APPROVAL
    assert d.update(REAL_TRUST_PROMPT, now=2) is None
    d.update(WORKING, now=3)
    assert d.update(WORKING, now=4).event_type == E.RUNNING
    d.update(REAL_IDLE, now=5)
    assert d.update(REAL_IDLE, now=6).event_type == E.IDLE
    assert d.update(REAL_IDLE, now=100) is None


@pytest.mark.parametrize("code,state", [(0, E.COMPLETED), (1, E.ERROR), (127, E.ERROR), (130, E.INTERRUPTED), (143, E.INTERRUPTED)])
def test_real_process_exit_has_terminal_precedence(code, state):
    d = EventDetector("s", default_plugins()["codex"])
    result = d.update("", exit_code=code, structured={"type": "turn.started"})
    assert result.event_type == state
    assert str(code) in result.reason
    assert d.update("", exit_code=code) is None
    assert d.update("", structured={"type": "turn.started"}) is None


def test_code_blocks_error_words_and_unverified_screens_are_unknown():
    for plugin in default_plugins().values():
        d = EventDetector("s", plugin, stable_after=0)
        text = 'Example error:\n```\nDo you want to proceed?\n> Yes\nNavigate · confirm\n```'
        assert d.update(text) is None
        assert d.state == E.UNKNOWN


def test_stale_revisions_and_time_cannot_change_state():
    d = EventDetector("s", default_plugins()["codex"])
    d.update("", revision=5, now=5, structured={"type": "turn.completed"})
    assert d.update("", revision=4, now=6, structured={"type": "turn.started"}) is None
    assert d.update("", revision=6, now=4, structured={"type": "turn.started"}) is None
    assert d.state == E.COMPLETED


def test_notifications_preserve_failures_deduplicate_and_prioritize():
    d = EventDetector("s", default_plugins()["codex"])
    outbox = EventOutbox()
    completed = d.update("", now=1, structured={"type": "turn.completed"})
    failed = d.update("", now=2, structured={"type": "turn.failed"})
    outbox.put(completed)
    outbox.put(completed)
    outbox.put(failed)
    assert len(outbox) == 2
    assert outbox.peek() is failed
    assert outbox.peek() is failed  # unsuccessful delivery has no acknowledgement
    outbox.acknowledge(failed)
    assert outbox.peek() is completed


def test_malformed_or_unrecognized_structured_events_remain_conservative():
    for plugin in default_plugins().values():
        assert plugin.detect_structured({"type": {"unexpected": True}}).event_type == E.UNKNOWN
        assert plugin.detect_structured({"type": "session.status", "properties": {"status": {"type": []}}}).event_type == E.UNKNOWN
    d = EventDetector("s", default_plugins()["codex"])
    d.update("", now=1, structured={"type": "turn.started"})
    assert d.update("", now=2, structured={"type": "item.updated"}) is None
    assert d.state == E.RUNNING

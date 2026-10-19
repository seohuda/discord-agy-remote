import json
from pathlib import Path

import pytest
from test_alerts import REAL_IDLE, REAL_TRUST_PROMPT, WORKING

from dishell.events import EventDetector, EventOutbox
from dishell.events import EventType as E
from dishell.plugins import default_plugins

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


def outbox_event(sequence, state=E.RUNNING, session_id="codex-outbox"):
    from dishell.events import CLIEvent
    return CLIEvent(session_id, "codex", state, 0.0, 1.0, "fixture", sequence)


def test_outbox_acknowledge_cleans_pending_dedup_state():
    outbox = EventOutbox()
    event = outbox_event(1)
    outbox.put(event)
    outbox.acknowledge(event)
    assert len(outbox) == 0
    assert outbox._keys == set()


def test_outbox_duplicate_enqueue_and_retry_preserve_the_pending_event():
    from dataclasses import replace
    outbox = EventOutbox()
    event = outbox_event(1, E.ERROR)
    outbox.put(event)
    for _ in range(10):
        # Failure means no acknowledge. Retries may reconstruct the same event.
        assert outbox.peek() is event
        outbox.put(event)
        outbox.put(replace(event))
        assert len(outbox) == len(outbox._keys) == 1
    outbox.acknowledge(event)
    assert not outbox._keys


def test_outbox_priority_and_fifo_survive_out_of_order_acknowledgement():
    outbox = EventOutbox()
    running = outbox_event(1)
    completed = outbox_event(2, E.COMPLETED)
    approval = outbox_event(3, E.WAITING_APPROVAL)
    error = outbox_event(4, E.ERROR)
    interrupted = outbox_event(5, E.INTERRUPTED)
    for event in (running, completed, approval, error, interrupted):
        outbox.put(event)
    # A lower-priority send can complete after a new important event arrives.
    outbox.acknowledge(completed)
    for expected in (approval, error, interrupted, running):
        assert outbox.peek() is expected
        outbox.acknowledge(expected)
    assert len(outbox) == len(outbox._keys) == 0


def test_outbox_wrong_acknowledge_cannot_remove_or_poison_an_event():
    from dataclasses import replace
    outbox = EventOutbox()
    event, other = outbox_event(1), outbox_event(2, E.ERROR)
    outbox.put(event)
    outbox.acknowledge(replace(event))  # Equal values, but not the sent object.
    outbox.acknowledge(other)  # Never enqueued.
    assert outbox.peek() is event
    assert outbox._keys == {(event.session_id, event.sequence)}
    outbox.put(other)
    assert outbox.peek() is other
    assert len(outbox) == 2


def test_outbox_recent_acknowledgements_suppress_late_duplicates_with_bounded_lru():
    from dataclasses import replace

    from dishell.events import RECENT_ACK_LIMIT
    outbox = EventOutbox()
    for sequence in range(RECENT_ACK_LIMIT):
        event = outbox_event(sequence)
        outbox.put(event)
        outbox.acknowledge(event)
    # Refresh one old key's recency, then add another acknowledgement.
    outbox.put(outbox_event(0))
    assert not len(outbox)
    newest = outbox_event(RECENT_ACK_LIMIT)
    outbox.put(newest)
    outbox.acknowledge(newest)
    outbox.put(replace(newest))
    outbox.acknowledge(newest)  # Duplicate ack must not disturb other keys.
    assert not len(outbox)
    assert not outbox._keys
    assert len(outbox._recent) == RECENT_ACK_LIMIT
    assert (newest.session_id, 0) in outbox._recent
    assert (newest.session_id, 1) not in outbox._recent
    # The finite replay window intentionally forgets the least recently used key.
    outbox.put(outbox_event(1))
    assert len(outbox) == 1


def test_outbox_dedup_memory_remains_bounded_after_twenty_thousand_deliveries():
    from dishell.events import RECENT_ACK_LIMIT
    outbox = EventOutbox()
    failed = outbox_event(-1, E.ERROR)
    outbox.put(failed)
    for sequence in range(20000):
        event = outbox_event(sequence)
        outbox.put(event)
        outbox.acknowledge(event)
        assert outbox._keys == {(failed.session_id, failed.sequence)}
        assert len(outbox._recent) <= RECENT_ACK_LIMIT
        assert not outbox._keys.intersection(outbox._recent)
    # A permanently failed notification is never evicted by the recent cache.
    assert outbox.peek() is failed
    assert len(outbox) == 1
    outbox.acknowledge(failed)
    assert len(outbox) == len(outbox._keys) == 0
    assert len(outbox._recent) == RECENT_ACK_LIMIT


def test_outbox_wrong_old_ack_cannot_remove_a_new_object_with_evicted_key():
    from dataclasses import replace

    from dishell.events import RECENT_ACK_LIMIT
    outbox = EventOutbox()
    old = outbox_event(0)
    for sequence in range(RECENT_ACK_LIMIT + 1):
        event = old if sequence == 0 else outbox_event(sequence)
        outbox.put(event)
        outbox.acknowledge(event)
    retry = replace(old)
    outbox.put(retry)
    outbox.acknowledge(old)
    assert outbox.peek() is retry
    assert len(outbox) == len(outbox._keys) == 1


@pytest.mark.parametrize("plugin_id", ["agy", "codex", "claude", "opencode"])
def test_plugins_ignore_non_object_structured_input(plugin_id):
    plugin = default_plugins()[plugin_id]
    for malformed in (None, [], ["result"], 7, "error"):
        assert plugin.detect_structured(malformed).event_type == E.UNKNOWN
        detector = EventDetector("schema-test", plugin)
        assert detector.update("", structured=malformed) is None
        assert detector.state == E.UNKNOWN


@pytest.mark.parametrize("event", [
    {"type": "result", "is_error": False},
    {"type": "result", "subtype": "future_result", "is_error": False},
    {"type": "result", "subtype": ["success"], "is_error": False},
    {"type": "result", "subtype": "success", "is_error": "false"},
    {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": []},
])
def test_claude_unknown_or_malformed_results_do_not_establish_success(event):
    detector = EventDetector("claude-malformed", default_plugins()["claude"])
    assert detector.update("", structured=event) is None
    assert detector.state == E.UNKNOWN


@pytest.mark.parametrize("event,state", [
    ({"type": "result", "subtype": "success", "is_error": False}, E.COMPLETED),
    ({"type": "result", "subtype": "error_during_execution", "is_error": True}, E.ERROR),
    ({"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "aborted_tools"}, E.INTERRUPTED),
])
def test_claude_explicit_success_error_and_interruption_remain_distinct(event, state):
    detector = EventDetector("claude-result", default_plugins()["claude"])
    assert detector.update("", structured=event).event_type == state


def test_exit_codes_override_pending_cli_approval_or_structured_success_for_all_plugins():
    for plugin in default_plugins().values():
        for code, state in ((0, E.COMPLETED), (7, E.ERROR), (130, E.INTERRUPTED)):
            detector = EventDetector("exit-test", plugin)
            event = detector.update(REAL_TRUST_PROMPT, structured={"type": "turn.completed"}, exit_code=code)
            assert event.event_type == state
            assert detector.update(REAL_TRUST_PROMPT, exit_code=code) is None


def test_notification_reasons_redact_environment_credentials_and_quoted_secrets(monkeypatch):
    monkeypatch.setenv("REVIEW_API_KEY", "synthetic-private-credential")
    detector = EventDetector("codex-mask", default_plugins()["codex"])
    event = detector.update("", structured={"type": "turn.failed", "error": {
        "message": "synthetic-private-credential password='two word password' Bearer synthetic-bearer"
    }})
    assert event.event_type == E.ERROR
    assert all(value not in event.reason for value in ("synthetic-private-credential", "two word password", "synthetic-bearer"))
    assert "[REDACTED]" in event.reason

from dishell.alerts import APPROVAL, DONE, AlertDetector, classify

WORKING = "● Reading main.py...\n\nesc to interrupt"
PROMPT = "Do you want to proceed?\n> Yes, and always allow 'ls'\n  No, and tell agy what to do"
QUIET = "> \n? for shortcuts"

# Captured verbatim from a real agy run through the wrapper.
REAL_TRUST_PROMPT = """Accessing workspace:

/tmp/scratchpad

Do you trust the contents of this project?

Antigravity CLI requires permission to read, edit, and execute files here.

> Yes, I trust this folder
  No, exit

  ↑/↓ Navigate · enter Confirm
                                                          Gemini 3.1 Pro · hig"""

REAL_IDLE = """      ▄▀▀▄        Antigravity CLI 1.1.7
     ▀▀▀▀▀▀       Gemini 3.1 Pro (High)
   ▄▀▀    ▀▀▄     ~/discord-agy-remote

──────────────────────────────────────────
>
──────────────────────────────────────────
? for shortcuts                Gemini 3.1 Pro · hig"""


def test_real_trust_prompt_is_detected_as_approval():
    assert classify(REAL_TRUST_PROMPT) == APPROVAL


def test_real_idle_screen_is_not_mistaken_for_a_prompt():
    assert classify(REAL_IDLE) == "idle"


def test_choice_footer_catches_prompts_with_unknown_wording():
    unknown = "Some future question?\n> Option A\n  Option B\n\n↑/↓ Navigate · enter Confirm"
    assert classify(unknown) == APPROVAL


def test_classify_recognises_the_three_states():
    assert classify(WORKING) == "busy"
    assert classify(PROMPT) == APPROVAL
    assert classify(QUIET) == "idle"


def test_classify_is_case_insensitive():
    assert classify("ESC TO INTERRUPT") == "busy"


def test_every_known_prompt_wording_is_detected():
    for prompt in (
        "Allow access to this file?",
        "Allow creation of this file?",
        "Accept this file edit?",
        "Do you want to proceed?",
    ):
        assert classify(prompt) == APPROVAL


def test_approval_fires_once_on_entry():
    det = AlertDetector()
    assert det.update(WORKING, 0.0) is None
    assert det.update(PROMPT, 1.0) == APPROVAL
    assert det.update(PROMPT, 2.0) is None  # still waiting, do not nag


def test_approval_fires_again_for_a_second_prompt():
    det = AlertDetector(idle_after=1.0)
    det.update(PROMPT, 0.0)
    det.update(WORKING, 1.0)
    assert det.update(PROMPT, 2.0) == APPROVAL


def test_done_fires_after_work_stops_and_the_screen_settles():
    det = AlertDetector(idle_after=3.0)
    det.update(WORKING, 0.0)
    assert det.update(QUIET, 1.0) is None  # not settled yet
    assert det.update(QUIET, 3.0) is None
    assert det.update(QUIET, 4.5) == DONE
    assert det.update(QUIET, 9.0) is None  # only once


def test_a_repaint_during_the_settle_window_restarts_the_timer():
    det = AlertDetector(idle_after=3.0)
    det.update(WORKING, 0.0)
    det.update(QUIET, 1.0)
    det.update(QUIET + "\nredrawn", 3.0)  # screen changed, timer resets
    assert det.update(QUIET + "\nredrawn", 5.0) is None
    assert det.update(QUIET + "\nredrawn", 6.5) == DONE


def test_idle_without_preceding_work_never_reports_done():
    det = AlertDetector(idle_after=1.0)
    for tick in range(10):
        assert det.update(QUIET, float(tick)) is None


def test_a_prompt_cancels_a_pending_completion():
    det = AlertDetector(idle_after=3.0)
    det.update(WORKING, 0.0)
    det.update(QUIET, 1.0)
    assert det.update(PROMPT, 2.0) == APPROVAL
    det.update(QUIET, 3.0)
    # The pending "done" was dropped; nothing fires until work runs again.
    assert det.update(QUIET, 20.0) is None


def test_full_run_emits_prompt_then_completion():
    det = AlertDetector(idle_after=2.0)
    events = []
    for snapshot, now in (
        (QUIET, 0.0),
        (WORKING, 1.0),
        (PROMPT, 2.0),
        (WORKING, 3.0),
        (QUIET, 4.0),
        (QUIET, 7.0),
    ):
        event = det.update(snapshot, now)
        if event:
            events.append(event)
    assert events == [APPROVAL, DONE]

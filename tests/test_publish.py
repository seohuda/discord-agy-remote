"""The publishing path: one live message that is edited, new ones on alerts."""

import asyncio

import pytest

from dishell.manager import DishellManager


class FakeMessage:
    def __init__(self, content, embed):
        self.content = content
        self.embed = embed
        self.deleted = False
        self.edits = 0

    async def edit(self, embed=None):
        self.embed = embed
        self.edits += 1

    async def delete(self):
        self.deleted = True


class FakeUser:
    def __init__(self):
        self.messages: list[FakeMessage] = []

    async def send(self, content=None, embed=None):
        message = FakeMessage(content, embed)
        self.messages.append(message)
        return message


@pytest.fixture
def manager():
    manager = DishellManager(token="t", user_id=1, argv=["agy"])
    manager._user = FakeUser()
    return manager


def run(coro):
    return asyncio.run(coro)


def test_the_first_screen_is_sent_as_a_new_message(manager):
    run(manager._publish("hello"))
    assert len(manager._user.messages) == 1
    assert "hello" in manager._user.messages[0].embed.description


def test_a_following_screen_edits_the_same_message(manager):
    run(manager._publish("first"))
    run(manager._publish("second"))
    assert len(manager._user.messages) == 1
    assert manager._user.messages[0].edits == 1
    assert "second" in manager._user.messages[0].embed.description


def test_an_alert_starts_a_new_message_with_a_header(manager):
    run(manager._publish("first"))
    manager._start_new_message = True
    manager._pending_alert = "approval"
    run(manager._publish("waiting"))
    assert len(manager._user.messages) == 2
    assert "입력 대기" in manager._user.messages[1].content
    # The flags are one-shot: the next screen goes back to editing.
    assert manager._pending_alert is None
    run(manager._publish("still waiting"))
    assert len(manager._user.messages) == 2


def test_growing_output_adds_messages_and_shrinking_removes_them(manager):
    long_screen = "\n".join(f"line {i}" for i in range(2000))
    run(manager._publish(long_screen))
    assert len(manager._user.messages) > 1
    grew = list(manager._user.messages)

    run(manager._publish("short"))
    assert all(m.deleted for m in grew[1:])
    assert not grew[0].deleted
    assert len(manager._live_messages) == 1


def test_history_lines_are_published_above_the_screen(manager):
    run(manager._publish("now", ["scrolled off"]))
    description = manager._user.messages[0].embed.description
    assert description.index("scrolled off") < description.index("now")

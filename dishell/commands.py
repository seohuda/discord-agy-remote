"""Parsing of Discord DM content into actions against the local agy session.

Only ``/rc``, ``/key`` and ``/help`` are intercepted. Every other message --
including agy's own slash commands such as ``/model`` -- is forwarded to the
PTY as-is. A leading ``//`` escapes one slash, so ``//rc off`` types the literal
text ``/rc off`` into agy.
"""

from dataclasses import dataclass, field

from .keys import UnknownKey, key_names, resolve_keys

HELP_TEXT = (
    "**discord-agy-remote**\n"
    "· 일반 메시지 → agy 입력창에 입력 후 전송\n"
    "· `/key <이름>...` → 특수키 전송 (예: `/key esc`, `/key down down enter`)\n"
    "· `/rc off` → 원격 제어 종료\n"
    "· `/rc status` → 세션 상태 확인\n"
    "· `//텍스트` → 앞의 슬래시 하나를 없애고 그대로 전송\n"
    f"\n사용 가능한 키: `{'`, `'.join(key_names())}`"
)


@dataclass
class Action:
    """A parsed remote instruction.

    ``kind`` is one of ``text``, ``keys``, ``rc_off``, ``rc_status``, ``help``
    or ``error``.
    """

    kind: str
    payload: bytes = b""
    message: str = ""
    key_names: list[str] = field(default_factory=list)


def text_action(text: str) -> Action:
    """Type ``text`` into agy and submit it.

    Multi-line text is wrapped in a bracketed paste so the TUI inserts it as a
    single multi-line prompt instead of submitting each line separately.
    """
    if "\n" in text:
        body = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"
    else:
        body = text.encode("utf-8")
    return Action(kind="text", payload=body + b"\r", message=text)


def parse_remote_message(content: str) -> Action:
    """Turn the raw content of a Discord DM into an :class:`Action`."""
    text = content.rstrip("\n")
    if not text.strip():
        return Action(kind="error", message="빈 메시지는 전송하지 않습니다.")

    if text.startswith("//"):
        return text_action(text[1:])

    head, _, rest = text.partition(" ")
    head = head.lower()
    args = rest.split()

    if head == "/help":
        return Action(kind="help", message=HELP_TEXT)

    if head == "/rc":
        sub = args[0].lower() if args else ""
        if sub == "off":
            return Action(kind="rc_off")
        if sub == "status":
            return Action(kind="rc_status")
        if sub == "on":
            return Action(kind="error", message="원격 제어는 이미 켜져 있습니다.")
        return Action(kind="error", message="사용법: `/rc off` 또는 `/rc status`")

    if head == "/key":
        if not args:
            return Action(
                kind="error",
                message=f"사용법: `/key <이름>...`\n사용 가능한 키: `{'`, `'.join(key_names())}`",
            )
        try:
            payload = resolve_keys(args)
        except UnknownKey as exc:
            return Action(
                kind="error",
                message=f"알 수 없는 키: `{exc.args[0]}`\n사용 가능한 키: `{'`, `'.join(key_names())}`",
            )
        return Action(kind="keys", payload=payload, key_names=[a.lower() for a in args])

    return text_action(text)

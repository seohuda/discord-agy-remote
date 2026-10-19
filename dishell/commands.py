"""Parsing of Discord DM content into actions against the local agy session.

``/rc``, ``/key``, ``/say``, ``/session(s)`` and ``/help`` are intercepted. Other messages --
including agy's own slash commands such as ``/model`` -- is forwarded to the
PTY as-is. A leading ``//`` escapes one slash, so ``//rc off`` types the literal
text ``/rc off`` into agy.
"""

from dataclasses import dataclass, field

from .keys import UnknownKey, key_names, resolve_keys

HELP_TEXT = (
    "**discord-agy-remote**\n"
    "· 일반 메시지 → 활성 세션 입력 승인 요청 (기본 읽기 전용)\n"
    "· `/sessions` → 세션 목록\n"
    "· `/session start <agy|codex|claude|opencode>` → 세션 시작 승인 요청\n"
    "· `/session attach|status|stop <id>` → 연결·상태·종료\n"
    "· `/session history <id> [page]` → 스크롤 기록 (0=최근)\n"
    "· `/key <이름>...` → 특수키 전송 (예: `/key esc`, `/key down down enter`)\n"
    "· `/say <텍스트>` → 슬래시로 시작하는 내용도 그대로 입력\n"
    "· `/rc off` → 원격 제어 종료\n"
    "· `/rc status` → 세션 상태 확인\n"
    "· `//텍스트` → 앞의 슬래시 하나를 없애고 그대로 전송\n"
    "\n`/` 를 치면 뜨는 자동완성 목록에서 골라도 되고, 그냥 텍스트로 쳐도 됩니다.\n"
    "입력·키·관리 작업은 60초 이내 소유자 버튼 승인이 필요합니다. 자동 Enter는 없습니다.\n"
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
    arguments: tuple[str, ...] = ()


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

    if head == "/sessions":
        return Action(kind="sessions") if not args else Action(kind="error", message="사용법: `/sessions`")

    if head == "/session":
        usage = "사용법: `/session start <agy|codex|claude|opencode>` · `/session attach|status|stop|history <id>`"
        if len(args) == 2 and args[0] in ("start", "attach", "status", "stop"):
            if args[0] == "start" and args[1] not in ("agy", "codex", "claude", "opencode"):
                return Action(kind="error", message=usage)
            return Action(kind="session_" + args[0], arguments=tuple(args[1:]))
        if len(args) in (2, 3) and args[0] == "history":
            if len(args) == 2 or (args[2].isdigit() and len(args[2]) <= 5):
                return Action(kind="session_history", arguments=tuple(args[1:]))
        return Action(kind="error", message=usage)

    if head == "/say":
        return text_action(rest) if rest.strip() else Action(kind="error", message="사용법: `/say <텍스트>`")

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

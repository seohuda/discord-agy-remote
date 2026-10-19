"""Best-effort display redaction. Audits never accept prompt/output fields."""

import os
import re

_CREDENTIAL = re.compile(
    r"(?i)(\b(?:[\w.-]*(?:token|secret|password|api[_-]?key)[\w.-]*)\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[\w.\-]+")
_TOKEN = re.compile(r"\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]{8,})\b")


def redact(text: str) -> str:
    # Known environment secrets are replaced even when printed without a label.
    for name, value in os.environ.items():
        if len(value) >= 6 and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL", name, re.I):
            text = text.replace(value, "[REDACTED]")
    text = _CREDENTIAL.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _TOKEN.sub("[REDACTED]", text)

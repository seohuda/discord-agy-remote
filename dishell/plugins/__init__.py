"""Only locally configured, built-in plugins may be launched remotely."""
import os

from .agy import AgyPlugin
from .claude import ClaudePlugin
from .codex import CodexPlugin
from .opencode import OpenCodePlugin


def default_plugins():
    return {plugin.id: plugin for plugin in (
        AgyPlugin(os.getenv("DISHELL_AGY_BIN", "agy")),
        CodexPlugin(os.getenv("DISHELL_CODEX_BIN", "codex")),
        ClaudePlugin(os.getenv("DISHELL_CLAUDE_BIN", "claude")),
        OpenCodePlugin(os.getenv("DISHELL_OPENCODE_BIN", "opencode")),
    )}

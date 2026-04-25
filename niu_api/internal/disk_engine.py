"""Virtual Disk engine — Main orchestrator that ties parser, navigator, and executor together."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from niu_api.internal.disk_config import DiskConfig
from niu_api.internal.disk_executor import DiskExecutor
from niu_api.internal.disk_navigator import DiskNavigator
from niu_api.internal.disk_parser import DiskParser

logger = logging.getLogger(__name__)


@dataclass
class DiskResult:
    """Unified return type for disk operations."""
    action: str  # LIST, READ, HELP, EXECUTE, ERROR
    text: str = ""  # Navigation/error text (for LIST/READ/HELP/ERROR)
    raw_result: Any = None  # Raw MCP return value (for EXECUTE only)
    tool_path: str = ""  # Actual tool path (for EXECUTE), used by tool_after_callback


class DiskEngine:
    """Main orchestrator for the virtual disk.

    Synchronous interface — safe to call from handler.dispatch() which is
    a sync generator.
    """

    def __init__(self, config_dir: str, registry=None):
        self.config = DiskConfig(config_dir, registry=registry)
        self.parser = DiskParser()
        self.navigator = DiskNavigator(self.config)
        self.executor = DiskExecutor(self.config, registry)
        self._registry = registry

    def execute(self, command: str) -> DiskResult:
        """Main entry point: parse command string, return DiskResult."""
        parsed = self.parser.parse(command)

        # Shell syntax errors
        if parsed.action == "SHELL_SYNTAX":
            return DiskResult(action="ERROR", text=parsed.error_msg)

        # Navigation commands (ls, cat, help, unknown, empty)
        if parsed.action in ("LIST", "READ", "HELP", "UNKNOWN", "EMPTY"):
            text = self.navigator.handle(parsed)
            return DiskResult(action=parsed.action, text=text)

        # Execute directory (E15)
        if parsed.action == "EXECUTE" and parsed.tool_name is None:
            text = self.navigator.handle(parsed)
            return DiskResult(action="ERROR", text=text)

        # Tool execution
        if parsed.action == "EXECUTE":
            exec_result = self.executor.execute(parsed)
            if exec_result.is_error:
                # Parameter/execution error → text
                return DiskResult(action="ERROR", text=exec_result.value,
                                  tool_path=f"/{parsed.dir_name}/{parsed.tool_name}")
            else:
                # MCP success/failure → raw result
                return DiskResult(action="EXECUTE", raw_result=exec_result.value,
                                  tool_path=f"/{parsed.dir_name}/{parsed.tool_name}")

        return DiskResult(action="ERROR", text=f"Unknown action: {parsed.action}")

    def get_schema(self) -> dict:
        """Return the disk() tool schema in OpenAI function-calling format.

        The description includes directory mapping so LLMs can skip the first
        exploration turn.
        """
        # Simplify: just use dir=server format
        dirs_short = ", ".join(
            f"{d}={sn.replace('-server', '')}"
            for d, sn in sorted(self.config.directory_map.items())
        )
        description = (
            f"Virtual tool disk — Unix-like shell to discover and execute tools. "
            f"Directories: {dirs_short}. "
            f"Commands: ls [path] list, cat <path> help, /<dir>/<tool> [args] execute."
        )

        return {
            "type": "function",
            "function": {
                "name": "disk",
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command: ls [path] list, cat <path> help, /<dir>/<tool> [args] execute",
                        },
                    },
                    "required": ["command"],
                },
            },
        }

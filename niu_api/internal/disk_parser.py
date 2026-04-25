"""Virtual Disk command parser — tokenization, syntax detection, action recognition."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Shell special syntax patterns (checked before tokenization)
# ---------------------------------------------------------------------------

# These patterns match special shell syntax OUTSIDE of quotes.
# We scan the raw command string for these and reject them early.

_SHELL_PATTERNS: list[tuple[str, str]] = [
    (r'(?<!\\)\|', "pipe"),
    (r'(?<!\\)&&', "chaining"),
    (r'(?<!\\);', "chaining"),
    (r'(?<!\\)>>?', "redirect"),
    (r'(?<!\\)\*', "wildcard"),
    (r'(?<!\\)\?', "wildcard"),
    (r'(?<!\\)\$', "variable"),
    (r'(?<!\\)&', "background"),
]

_SHELL_ERROR_MSGS = {
    "pipe": "|: pipe syntax not supported. This shell executes one tool at a time.",
    "chaining": "&&: command chaining not supported. Execute one tool at a time.",
    "redirect": ">: redirection not supported in this shell.",
    "wildcard": "*: wildcard not supported. Use exact tool names.",
    "variable": "$: variable expansion not supported. Use literal values.",
    "background": "&: background execution not supported.",
}


def _strip_quoted_regions(cmd: str) -> str:
    """Remove content inside quotes so we only detect special syntax outside quotes."""
    result = []
    in_single = False
    in_double = False
    for ch in cmd:
        if ch == "'" and not in_double:
            in_single = not in_single
            result.append(" ")
        elif ch == '"' and not in_single:
            in_double = not in_double
            result.append(" ")
        elif in_single or in_double:
            result.append(" ")  # Replace quoted char with space
        else:
            result.append(ch)
    return "".join(result)


def _check_shell_syntax(cmd: str) -> str | None:
    """Check for shell special syntax outside quotes. Returns error msg or None."""
    stripped = _strip_quoted_regions(cmd)
    for pattern, syntax_type in _SHELL_PATTERNS:
        if re.search(pattern, stripped):
            return _SHELL_ERROR_MSGS[syntax_type]
    return None


# ---------------------------------------------------------------------------
# Parsed command result
# ---------------------------------------------------------------------------

@dataclass
class ParsedCommand:
    """Result of parsing a disk command string."""
    action: str  # LIST, READ, HELP, EXECUTE, UNKNOWN, EMPTY, SHELL_SYNTAX
    raw: str = ""
    path: str = "/"
    dir_name: str | None = None
    tool_name: str | None = None
    positional_args: list[str] = field(default_factory=list)
    flag_args: dict[str, str | bool] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)  # ls --all etc.
    error_msg: str = ""


# ---------------------------------------------------------------------------
# DiskParser
# ---------------------------------------------------------------------------

class DiskParser:
    """Parse disk command strings into structured ParsedCommand objects."""

    def tokenize(self, command: str) -> tuple[list[str], int | None]:
        """Shell-style tokenization respecting quotes and -- terminator.

        Returns (tokens, terminator_index) where terminator_index is the
        index in tokens where post-terminator args begin, or None if no --.
        """
        command = command.strip()
        if not command:
            return [], None

        # Find -- terminator position in raw string (outside quotes)
        terminator_idx = self._find_double_dash(command)
        if terminator_idx is not None:
            pre = command[:terminator_idx].strip()
            post = command[terminator_idx + 2:].strip()
            try:
                pre_tokens = shlex.split(pre, posix=True) if pre else []
            except ValueError:
                pre_tokens = pre.split()
            # Post-terminator: simple split, all treated as positional
            try:
                post_tokens = shlex.split(post, posix=True) if post else []
            except ValueError:
                post_tokens = post.split()
            terminator_at = len(pre_tokens)
            return pre_tokens + post_tokens, terminator_at
        else:
            try:
                return shlex.split(command, posix=True), None
            except ValueError:
                return command.split(), None

    @staticmethod
    def _find_double_dash(command: str) -> int | None:
        """Find standalone -- outside of quotes. Returns index or None."""
        in_single = False
        in_double = False
        i = 0
        while i < len(command):
            ch = command[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                # Check for standalone --
                if (ch == '-' and i + 1 < len(command) and command[i + 1] == '-'
                        and (i + 2 >= len(command) or command[i + 2] in (' ', '\t'))
                        and (i == 0 or command[i - 1] in (' ', '\t'))):
                    return i
            i += 1
        return None

    def _parse_args(self, tokens: list[str], terminator_at: int | None = None
                    ) -> tuple[list[str], dict[str, str | bool]]:
        """Parse positional args and --flag args from token list.

        If terminator_at is given, tokens at index >= terminator_at-1
        (adjusted for being in the 'rest' slice) are treated as positional.
        """
        positional = []
        flag_args = {}

        # Calculate the boundary: after --, everything is positional
        # terminator_at is the index in the full token list where -- was.
        # In the 'rest' slice (tokens = full[1:]), the boundary is terminator_at - 1.
        post_term_start = None
        if terminator_at is not None and terminator_at > 0:
            post_term_start = terminator_at - 1  # adjust for rest slice

        i = 0
        while i < len(tokens):
            # After terminator, all tokens are positional
            if post_term_start is not None and i >= post_term_start:
                positional.append(tokens[i])
                i += 1
                continue

            token = tokens[i]
            if token.startswith("--") and len(token) > 2:
                # Flag argument
                flag_part = token[2:]
                if "=" in flag_part:
                    key, val = flag_part.split("=", 1)
                    flag_args[key] = val
                elif i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    flag_args[flag_part] = tokens[i + 1]
                    i += 1
                else:
                    # Boolean flag
                    flag_args[flag_part] = True
            elif token.startswith("-") and len(token) > 1 and not token[1:].isdigit():
                # Short flag
                flag_part = token[1:]
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    flag_args[flag_part] = tokens[i + 1]
                    i += 1
                else:
                    flag_args[flag_part] = True
            else:
                positional.append(token)
            i += 1

        return positional, flag_args

    def _parse_path(self, path: str) -> tuple[str | None, str | None]:
        """Parse a /dir/tool path into (dir_name, tool_name)."""
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 0:
            return None, None
        elif len(parts) == 1:
            return parts[0], None
        else:
            return parts[0], parts[1]

    def parse(self, command: str) -> ParsedCommand:
        """Parse a disk command string into a ParsedCommand."""
        command = command.strip()

        # Empty command
        if not command:
            return ParsedCommand(action="EMPTY")

        # Check shell special syntax first
        error = _check_shell_syntax(command)
        if error:
            return ParsedCommand(action="SHELL_SYNTAX", raw=command, error_msg=error)

        tokens, terminator_at = self.tokenize(command)
        if not tokens:
            return ParsedCommand(action="EMPTY")

        first = tokens[0]
        rest = tokens[1:]

        # Built-in navigation commands
        if first == "ls":
            # Parse ls [--all] [path]
            all_flag = False
            path = "/"
            for t in rest:
                if t == "--all":
                    all_flag = True
                elif t.startswith("-"):
                    pass  # ignore unknown flags for ls
                else:
                    path = t if t.startswith("/") else f"/{t}"
            result = ParsedCommand(action="LIST", raw=command, path=path)
            if all_flag:
                result.flags["all"] = True
            return result

        if first == "cat":
            if not rest:
                return ParsedCommand(action="UNKNOWN", raw=command, error_msg="cat: missing path argument")
            path = rest[0] if rest[0].startswith("/") else f"/{rest[0]}"
            return ParsedCommand(action="READ", raw=command, path=path)

        if first == "help":
            return ParsedCommand(action="HELP", raw=command)

        # Tool execution: starts with /
        if first.startswith("/"):
            path = first
            dir_name, tool_name = self._parse_path(path)
            positional, flag_args = self._parse_args(rest, terminator_at)
            return ParsedCommand(
                action="EXECUTE",
                raw=command,
                path=path,
                dir_name=dir_name,
                tool_name=tool_name,
                positional_args=positional,
                flag_args=flag_args,
            )

        # Unknown command
        return ParsedCommand(action="UNKNOWN", raw=command)

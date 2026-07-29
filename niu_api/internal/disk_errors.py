"""Virtual Disk error templates and formatting."""

from __future__ import annotations

from typing import Any

from niu_api.internal.disk_config import ArgConfig, ToolConfig

# ---------------------------------------------------------------------------
# Fuzzy matching for flag suggestions
# ---------------------------------------------------------------------------

class FuzzyMatcher:
    """Suggest the closest flag name when the user makes a typo."""

    MAX_DISTANCE = 2

    @staticmethod
    def suggest(typed: str, candidates: list[str]) -> str | None:
        """Return the best candidate within Levenshtein distance, or None."""
        if not typed or not candidates:
            return None
        best = None
        best_dist = FuzzyMatcher.MAX_DISTANCE + 1
        for c in candidates:
            d = FuzzyMatcher._levenshtein(typed, c)
            if d < best_dist:
                best_dist = d
                best = c
        if best_dist <= FuzzyMatcher.MAX_DISTANCE:
            return best
        return None

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """Compute Levenshtein distance between two strings."""
        if len(a) < len(b):
            return FuzzyMatcher._levenshtein(b, a)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                cost = 0 if ca == cb else 1
                curr.append(min(
                    curr[j] + 1,
                    prev[j + 1] + 1,
                    prev[j] + cost,
                ))
            prev = curr
        return prev[-1]


# ---------------------------------------------------------------------------
# Usage formatting helpers
# ---------------------------------------------------------------------------

def _format_usage_line(dir_name: str, tool: ToolConfig) -> str:
    """Format the USAGE line for a tool."""
    parts = [f"/{dir_name}/{tool.name}"]
    for arg in tool.args:
        if arg.position is not None:
            if arg.required:
                parts.append(f"<{arg.name}>")
            else:
                parts.append(f"[{arg.name}]")
    if any(a.position is None and not a.required for a in tool.args):
        parts.append("[options]")
    return " ".join(parts)


def _format_args_section(tool: ToolConfig) -> str:
    """Format the ARGUMENTS section (positional only)."""
    positional = [a for a in tool.args if a.position is not None]
    if not positional:
        return ""
    lines = []
    for arg in sorted(positional, key=lambda a: a.position):
        req = "" if arg.required else " (optional)"
        lines.append(f"  {arg.name:20s} {arg.description}{req}")
    return "ARGUMENTS:\n" + "\n".join(lines)


def _format_options_section(tool: ToolConfig) -> str:
    """Format the OPTIONS section (flags only)."""
    flags = [a for a in tool.args if a.position is None]
    if not flags:
        return ""
    lines = []
    for arg in flags:
        flag_str = f"--{arg.flag}"
        type_hint = _type_hint(arg)
        default_str = f" (default: {arg.default})" if arg.has_default else ""
        enum_str = ""
        if arg.enum:
            enum_str = f": {'|'.join(arg.enum)}"
        lines.append(f"  {flag_str} {type_hint:12s} {arg.description}{enum_str}{default_str}")
    return "OPTIONS:\n" + "\n".join(lines)


def _type_hint(arg: ArgConfig) -> str:
    """Get a short type hint string for display."""
    if arg.type == "boolean":
        return ""
    if arg.type == "integer":
        return "N"
    if arg.type == "number":
        return "N"
    return "VAL"


def _format_examples_section(tool: ToolConfig) -> str:
    """Format the EXAMPLES section."""
    if not tool.examples:
        return ""
    lines = ["EXAMPLES:"]
    for ex in tool.examples:
        lines.append(f"  {ex}")
    return "\n".join(lines)


def _format_full_usage(dir_name: str, tool: ToolConfig) -> str:
    """Format complete self-contained usage info (for E5 and E17)."""
    sections = [f"Usage: {_format_usage_line(dir_name, tool)}"]
    args_section = _format_args_section(tool)
    if args_section:
        sections.append(args_section)
    opts_section = _format_options_section(tool)
    if opts_section:
        sections.append(opts_section)
    examples = _format_examples_section(tool)
    if examples:
        sections.append(examples)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# DiskErrors — all error message generators
# ---------------------------------------------------------------------------

class DiskErrors:
    """Generate all error messages for the virtual disk."""

    def unknown_command(self, cmd: str) -> str:
        """E1: Unknown command."""
        return (
            f"{cmd}: command not found. "
            f"Available commands: ls, cat, help.\n"
            f"To execute a tool: /<server>/<tool> [args]"
        )

    def path_not_found(self, path: str, available_dirs: list[str],
                       command: str = "ls") -> str:
        """E2: Path does not exist."""
        dirs = ", ".join(available_dirs)
        return (
            f"{command}: {path}: No such file or directory.\n"
            f"Available directories: {dirs}"
        )

    def tool_not_found(self, dir_name: str, tool_name: str,
                       available_tools: list[str],
                       command: str = "cat") -> str:
        """E3: Tool not found — list ALL tools, no truncation."""
        tools = ", ".join(available_tools)
        return (
            f"{command}: /{dir_name}/{tool_name}: No such file.\n"
            f"Available tools in /{dir_name}: {tools}"
        )

    def is_directory(self, path: str) -> str:
        """E4: Trying to cat a directory."""
        return (
            f"cat: {path}: Is a directory.\n"
            f"Use 'ls {path}' to list tools, or 'cat {path}/<tool>' for usage."
        )

    def missing_required_arg(self, dir_name: str, tool: ToolConfig,
                             missing_arg: str) -> str:
        """E5: Missing required argument — self-contained with ALL params."""
        header = f"/{dir_name}/{tool.name}: missing required argument <{missing_arg}>."
        usage = _format_full_usage(dir_name, tool)
        example = tool.examples[0] if tool.examples else ""
        example_line = f"\n\nEXAMPLE:\n  {example}" if example else ""
        return f"{header}\n\n{usage}{example_line}"

    def type_mismatch(self, tool_path: str, flag: str,
                      expected_type: str, value: str) -> str:
        """E6: Argument type mismatch."""
        return (
            f"{tool_path}: invalid value for --{flag}: "
            f"expected {expected_type}, got '{value}'."
        )

    def unknown_flag(self, dir_name: str, tool: ToolConfig,
                     flag: str, available_flags: list[str]) -> str:
        """E7: Unknown flag with fuzzy match suggestion."""
        suggestion = FuzzyMatcher.suggest(flag, available_flags)
        header = f"/{dir_name}/{tool.name}: unknown option '--{flag}'."
        parts = [header]
        if suggestion:
            parts.append(f"Did you mean --{suggestion}?")
        parts.append(f"Available options: {', '.join('--' + f for f in available_flags)}")
        return "\n".join(parts)

    def enum_error(self, tool_path: str, flag: str,
                   value: str, valid_values: list[str]) -> str:
        """E8: Invalid enum value."""
        return (
            f"{tool_path}: invalid value for --{flag}: '{value}'.\n"
            f"Must be one of: {', '.join(valid_values)}"
        )

    def out_of_range(self, tool_path: str, flag: str,
                     value: Any, *, minimum=None, maximum=None) -> str:
        """E9: Value out of range."""
        if minimum is not None and maximum is not None:
            range_str = f"{minimum}-{maximum}"
        elif minimum is not None:
            range_str = f"≥{minimum}"
        else:
            range_str = f"≤{maximum}"
        return (
            f"{tool_path}: --{flag} value {value} out of range. "
            f"Must be {range_str}."
        )

    def too_many_args(self, tool_path: str, expected: int, actual: int) -> str:
        """E10: Too many positional arguments."""
        return (
            f"{tool_path}: too many arguments. "
            f"Expected {expected} positional, got {actual}."
        )

    def empty_command(self) -> str:
        """E11: Empty command — show usage."""
        return (
            "Usage: disk <command>\n\n"
            "Commands:\n"
            "  ls [path]         List directories and tools\n"
            "  cat <path>        Read tool usage (README)\n"
            "  help              Show this help\n"
            "  /<dir>/<tool>     Execute a tool\n\n"
            "Start with: ls /"
        )

    def execution_failure(self, tool_path: str, error: str) -> str:
        """E12: MCP tool execution failure."""
        return (
            f"{tool_path}: execution failed.\n{error}"
        )

    def cd_not_supported(self) -> str:
        """E13: cd not supported."""
        return (
            "cd: not supported. This shell does not maintain working directory.\n"
            "Use absolute paths: /<dir>/<tool> [args]\n"
            "Use ls /<dir> to explore available tools."
        )

    def flag_missing_value(self, tool_path: str, flag: str) -> str:
        """E14: Flag requires a value."""
        return (
            f"{tool_path}: --{flag} requires a value."
        )

    def execute_directory(self, path: str) -> str:
        """E15: Trying to execute a directory."""
        # Normalize: avoid // in suggestion when path is /
        norm = path.rstrip("/")
        return (
            f"{path}: is a directory, not a tool.\n"
            f"Use 'ls {path}' to list tools, or '{norm}/<tool>' to execute."
        )

    def shell_syntax(self, syntax_type: str) -> str:
        """E16: Shell special syntax detected."""
        msgs = {
            "or_operator": "||: OR operator not supported. This shell executes one tool at a time.",
            "pipe": "|: pipe syntax not supported. This shell executes one tool at a time.",
            "and_operator": "&&: command chaining not supported. Execute one tool at a time.",
            "semicolon": ";: command chaining not supported. Execute one tool at a time.",
            "redirect": ">: redirection not supported in this shell.",
            "wildcard": "*: wildcard not supported. Use exact tool names.",
            "variable": "$: variable expansion not supported. Use literal values.",
            "background": "&: background execution not supported.",
        }
        return msgs.get(syntax_type, "Unsupported shell syntax.")

    def repeated_errors(self, dir_name: str, tool: ToolConfig) -> str:
        """E17: Repeated errors escalation — inline full usage."""
        header = f"/{dir_name}/{tool.name}: repeated errors. Full usage:"
        usage = _format_full_usage(dir_name, tool)
        return f"{header}\n\n{usage}\n\nPlease follow the USAGE line exactly."

    def mutually_exclusive(self, tool_path: str, flag_a: str, flag_b: str,
                           group: list[str]) -> str:
        """E18: Mutually exclusive parameters."""
        return (
            f"{tool_path}: --{flag_a} and --{flag_b} are mutually exclusive.\n"
            f"Provide only one of: {', '.join('--' + f for f in group)}"
        )

    def with_first_error_tip(self, msg: str, tool_path: str,
                             first_error: bool = True) -> str:
        """Append a tip about using 'cat' on first error."""
        if first_error:
            return f"{msg}\n\n(Tip: Use 'cat {tool_path}' to review full documentation before first use.)"
        return msg

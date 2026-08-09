"""Virtual Disk navigator — ls, cat, help commands and navigation errors."""

from __future__ import annotations

from niu_api.internal.disk_config import DiskConfig, ToolConfig
from niu_api.internal.disk_errors import DiskErrors
from niu_api.internal.disk_parser import ParsedCommand


class DiskNavigator:
    """Handle navigation commands: ls, cat, help, and related errors."""

    def __init__(self, config: DiskConfig):
        self.config = config
        self.errors = DiskErrors()

    def handle(self, parsed: ParsedCommand) -> str:
        """Route a parsed command to the appropriate handler, return text."""
        if parsed.action == "LIST":
            return self.list_dir(parsed.path, show_all=parsed.flags.get("all", False))
        elif parsed.action == "READ":
            return self.read_tool(parsed.path)
        elif parsed.action == "HELP":
            return self.help()
        elif parsed.action == "EMPTY":
            return self.errors.empty_command()
        elif parsed.action == "UNKNOWN":
            # Use parser's error_msg if available (e.g. cat validation errors)
            if parsed.error_msg:
                return parsed.error_msg
            first_word = parsed.raw.split()[0] if parsed.raw else ""
            if first_word == "cd":
                return self.errors.cd_not_supported()
            return self.errors.unknown_command(first_word)
        elif parsed.action == "EXECUTE" and parsed.tool_name is None:
            # Trying to execute a directory
            return self.errors.execute_directory(parsed.path)
        else:
            return self.errors.unknown_command(parsed.raw)

    # -------------------------------------------------------------------
    # ls
    # -------------------------------------------------------------------

    def list_dir(self, path: str, *, show_all: bool = False) -> str:
        """List contents of a directory path."""
        if path == "/":
            return self._list_root()
        return self._list_subdir(path, show_all=show_all)

    def _list_root(self) -> str:
        """List all top-level directories."""
        lines = ["/:"]
        for dir_name in sorted(self.config.directory_map.keys()):
            server_name = self.config.directory_map[dir_name]
            server = self.config.servers[server_name]
            lines.append(f"  {dir_name + '/':<16s} {server.description}")
        lines.append("")
        lines.append("Usage: cat /<dir>/readme.txt for overview, cat /<dir>/<tool> for details, /<dir>/<tool> [args] to execute")
        return "\n".join(lines)

    def _list_subdir(self, path: str, *, show_all: bool = False) -> str:
        """List tools in a subdirectory."""
        # Normalize path: /kg → kg
        dir_name = path.strip("/")
        server = self.config.get_server_by_dir(dir_name)
        if server is None:
            available = sorted(self.config.directory_map.keys())
            return self.errors.path_not_found(path, available)

        # Get tools
        if show_all:
            tools = self.config.list_all_tools(dir_name)
        else:
            tools = self.config.list_visible_tools(dir_name)

        if not tools:
            return f"/{dir_name}: (no tools available)\n\nUsage: ls / to list directories"

        # Group by category
        categories: dict[str, list[ToolConfig]] = {}
        for tool in tools:
            cat = tool.category or "general"
            if tool.hidden:
                cat = "hidden"
            categories.setdefault(cat, []).append(tool)

        # Format output
        lines = [f"/{dir_name}:"]
        if len(tools) <= 5 and not show_all:
            # Flat list for few tools
            for tool in tools:
                lines.append(f"  {tool.name:<24s} {tool.summary}")
        else:
            # Categorized list
            for cat in sorted(categories.keys()):
                if cat not in ("general",):
                    lines.append(f"  [{cat}]")
                for tool in categories[cat]:
                    lines.append(f"    {tool.name:<22s} {tool.summary}")

        lines.append("")
        lines.append(f"Usage: cat /{dir_name}/readme.txt for overview, /{dir_name}/<tool> [args] to execute")
        return "\n".join(lines)

    # -------------------------------------------------------------------
    # cat
    # -------------------------------------------------------------------

    def read_tool(self, path: str) -> str:
        """Read tool README (full usage documentation).

        Supports virtual 'readme.txt' files: cat /<dir>/readme.txt
        returns a directory overview with tool list and usage hints.
        """
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 0:
            return self.errors.path_not_found(path, sorted(self.config.directory_map.keys()),
                                              command="cat")
        if len(parts) == 1:
            # Trying to cat a directory
            dir_name = parts[0]
            if self.config.get_server_by_dir(dir_name):
                return self.errors.is_directory(f"/{dir_name}")
            return self.errors.path_not_found(f"/{dir_name}", sorted(self.config.directory_map.keys()),
                                              command="cat")

        dir_name, tool_name = parts[0], parts[1]

        # Virtual readme.txt: cat /<dir>/readme.txt → directory overview
        if tool_name in ("readme.txt", "README.txt", "readme", "README"):
            return self._format_dir_readme(dir_name)

        tool = self.config.get_tool_config(dir_name, tool_name)
        if tool is None:
            available = [t.name for t in self.config.list_visible_tools(dir_name)]
            return self.errors.tool_not_found(dir_name, tool_name, available)

        return self._format_readme(dir_name, tool)

    def _format_dir_readme(self, dir_name: str) -> str:
        """Format a directory README (virtual readme.txt file).

        Returns an overview with full tool usage (args + options) so the LLM
        can use tools without needing a separate cat /<dir>/<tool> call.
        """
        from niu_api.internal.disk_errors import (
            _format_examples_section,
            _format_usage_line,
        )

        server = self.config.get_server_by_dir(dir_name)
        if server is None:
            available = sorted(self.config.directory_map.keys())
            return self.errors.path_not_found(f"/{dir_name}", available)

        tools = self.config.list_visible_tools(dir_name)
        if not tools:
            return f"/{dir_name}/readme.txt\n\n(no tools available)"

        lines = [f"/{dir_name}/readme.txt"]
        lines.append(server.description)
        lines.append("")

        for tool in tools:
            # Tool header: name + summary
            header = f"/{dir_name}/{tool.name}"
            if tool.summary:
                header += f" — {tool.summary}"
            lines.append(header)

            # Long description（完整说明——readme 应最全面，不能只给 short+参数）
            if tool.description:
                lines.append(f"  {tool.description}")

            # Usage line
            lines.append(f"  Usage: {_format_usage_line(dir_name, tool)}")

            # Positional args (inline)
            positional = [a for a in tool.args if a.position is not None]
            if positional:
                for arg in sorted(positional, key=lambda a: a.position):
                    req = "" if arg.required else " (optional)"
                    default = f" [default: {arg.default}]" if arg.has_default and arg.default is not None else ""
                    desc = f" — {arg.description}" if arg.description else ""
                    lines.append(f"    {arg.name}: {arg.type}{desc}{req}{default}")

            # Flag args (inline)
            flags = [a for a in tool.args if a.position is None]
            if flags:
                for arg in flags:
                    flag_str = f"--{arg.flag}"
                    req = " required" if arg.required else ""
                    default = f" [default: {arg.default}]" if arg.has_default and arg.default is not None else ""
                    enum_str = f" choices: {arg.enum}" if arg.enum else ""
                    desc = f" — {arg.description}" if arg.description else ""
                    lines.append(f"    {flag_str} <{arg.type}>{desc}{req}{default}{enum_str}")

            # Examples
            examples = _format_examples_section(tool)
            if examples:
                for line in examples.split("\n"):
                    lines.append(f"  {line}")

            lines.append("")

        lines.append(f"Execute: /{dir_name}/<tool> <args>")
        return "\n".join(lines)

    def _format_readme(self, dir_name: str, tool: ToolConfig) -> str:
        """Format a tool README."""
        from niu_api.internal.disk_errors import (
            _format_args_section,
            _format_examples_section,
            _format_options_section,
            _format_usage_line,
        )

        title = f"/{dir_name}/{tool.name}"
        if tool.summary:
            title += f" — {tool.summary}"
        sections = [title]
        if tool.description:
            sections.append(tool.description)
        sections.append(f"USAGE:\n  {_format_usage_line(dir_name, tool)}")

        args = _format_args_section(tool)
        if args:
            sections.append(args)

        opts = _format_options_section(tool)
        if opts:
            sections.append(opts)

        examples = _format_examples_section(tool)
        if examples:
            sections.append(examples)

        return "\n\n".join(sections)

    # -------------------------------------------------------------------
    # help
    # -------------------------------------------------------------------

    def help(self) -> str:
        """Show general help."""
        return (
            "Virtual Disk — Unix-like shell to discover and execute tools.\n\n"
            "COMMANDS:\n"
            "  ls [path]           List directories and tools\n"
            "  ls --all <path>     List tools including hidden ones\n"
            "  cat <path>          Read tool usage (README)\n"
            "  help                Show this help\n"
            "  /<dir>/<tool>       Execute a tool\n\n"
            "GETTING STARTED:\n"
            "  ls /                List all service directories\n"
            "  ls /<dir>           List tools in a directory\n"
            "  cat /<dir>/<tool>   Read tool usage\n"
            "  /<dir>/<tool> [args]  Execute a tool\n\n"
            "DIRECTORIES:\n" +
            self._format_dir_list()
        )

    def _format_dir_list(self) -> str:
        """Format the directory list for help."""
        lines = []
        for dir_name in sorted(self.config.directory_map.keys()):
            server_name = self.config.directory_map[dir_name]
            server = self.config.servers[server_name]
            lines.append(f"  {dir_name + '/':<16s} {server.description}")
        return "\n".join(lines)

"""Virtual Disk configuration — YAML loading and validation."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

BUILTIN_COMMANDS = {"ls", "cat", "help", "cd", "pwd", "disk"}
VALID_ARG_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
TYPES_REQUIRING_CLI_FORMAT = {"object", "array"}


class ValidationError(Exception):
    """Raised when disk configuration fails validation."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ArgConfig:
    """Configuration for a single tool argument."""
    name: str
    type: str
    description: str
    position: int | None = None
    flag: str | None = None
    required: bool = False
    default: Any = None
    has_default: bool = False  # Distinguish "no default" from "default: null"
    enum: list[str] | None = None
    cli_format: str | None = None
    repeatable: bool = False
    items: dict | None = None
    sensitive: bool = False
    requires: str | None = None
    constraints: dict | None = None  # {minimum, maximum, pattern, max_length}

    def __post_init__(self):
        if self.flag is None:
            self.flag = self.name


@dataclass
class ToolConfig:
    """Configuration for a single MCP tool within the virtual disk."""
    name: str
    summary: str
    description: str
    args: list[ArgConfig]
    hidden: bool = False
    category: str | None = None
    mutually_exclusive: list[list[str]] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    """Configuration for a single MCP server (maps to a directory)."""
    server_name: str
    directory: str
    description: str
    tools: dict[str, ToolConfig]


# ---------------------------------------------------------------------------
# YAML parsing helpers
# ---------------------------------------------------------------------------

def _parse_arg(arg_data: dict) -> ArgConfig:
    """Parse a single arg definition from YAML."""
    has_default = "default" in arg_data
    return ArgConfig(
        name=arg_data["name"],
        type=arg_data["type"],
        description=arg_data.get("description", arg_data.get("desc", "")),
        position=arg_data.get("position"),
        flag=arg_data.get("flag"),
        required=arg_data.get("required", False),
        default=arg_data.get("default"),
        has_default=has_default,
        enum=arg_data.get("enum"),
        cli_format=arg_data.get("cli_format"),
        repeatable=arg_data.get("repeatable", False),
        items=arg_data.get("items"),
        sensitive=arg_data.get("sensitive", False),
        requires=arg_data.get("requires"),
        constraints=arg_data.get("constraints"),
    )


def _parse_tool(tool_name: str, tool_data: dict) -> ToolConfig:
    """Parse a single tool definition from YAML."""
    # Support both 'args' and 'parameters' keys
    raw_args = tool_data.get("args", tool_data.get("parameters", [])) or []
    args = [_parse_arg(a) for a in raw_args]
    return ToolConfig(
        name=tool_name,
        summary=tool_data.get("summary", tool_data.get("short", "")),
        description=tool_data.get("description", tool_data.get("long", "")),
        args=args,
        hidden=tool_data.get("hidden", False),
        category=tool_data.get("category"),
        mutually_exclusive=tool_data.get("mutually_exclusive", []),
        examples=tool_data.get("examples", []),
    )


def _parse_server(server_data: dict) -> ServerConfig:
    """Parse a single server definition from YAML."""
    tools = {}
    raw_tools = server_data.get("tools", {})
    if isinstance(raw_tools, list):
        # YAML list format: [{name: tool1, ...}, {name: tool2, ...}]
        for tool_data in raw_tools:
            tool_name = tool_data.get("name", "")
            if tool_name:
                tools[tool_name] = _parse_tool(tool_name, tool_data)
    elif isinstance(raw_tools, dict):
        # YAML dict format: {tool1: {summary: ...}, tool2: {summary: ...}}
        for tool_name, tool_data in raw_tools.items():
            tools[tool_name] = _parse_tool(tool_name, tool_data)
    return ServerConfig(
        server_name=server_data["server"],
        directory=server_data["directory"],
        description=server_data.get("description", ""),
        tools=tools,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_config(servers: dict[str, ServerConfig]) -> None:
    """Run all validation checks. Raises ValidationError on failure.

    Cross-file checks (duplicate directory names, reserved command conflicts,
    position gaps, etc.) remain strict — errors block startup. Per-yaml
    syntax errors are caught earlier in DiskConfig.__init__ with warning + skip.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Duplicate directory names
    dir_names: list[str] = []
    for s in servers.values():
        dir_names.append(s.directory)
    seen_dirs: set[str] = set()
    for d in dir_names:
        if d in seen_dirs:
            errors.append(f"Duplicate directory name: '{d}'")
        seen_dirs.add(d)

    # 2. Tool name / directory name conflicts with built-in commands
    for s in servers.values():
        if s.directory in BUILTIN_COMMANDS:
            errors.append(f"Directory '{s.directory}' conflicts with reserved command")
        for t_name in s.tools:
            if t_name in BUILTIN_COMMANDS:
                errors.append(f"Tool '{s.directory}/{t_name}' conflicts with reserved command")

    # 3-9: Per-server per-tool checks
    for s in servers.values():
        for t in s.tools.values():
            # 3. Position gaps
            positional = [(a.position, a) for a in t.args if a.position is not None]
            if positional:
                positional.sort(key=lambda x: x[0])
                positions = [p for p, _ in positional]
                for i, pos in enumerate(positions):
                    expected = i + 1
                    if pos != expected:
                        errors.append(
                            f"{s.directory}/{t.name}: position gap at {pos}, expected {expected}"
                        )
                        break

            # 4. Duplicate flag names within a tool
            flag_names: list[str] = []
            for a in t.args:
                if a.position is None:  # Only flag args have flags
                    flag_names.append(a.flag)
            seen_flags: set[str] = set()
            for f in flag_names:
                if f in seen_flags:
                    errors.append(f"{s.directory}/{t.name}: duplicate flag '--{f}'")
                seen_flags.add(f)

            # 5. object/array without cli_format
            for a in t.args:
                if a.type in TYPES_REQUIRING_CLI_FORMAT and not a.cli_format:
                    errors.append(
                        f"{s.directory}/{t.name}: arg '{a.name}' type={a.type} requires cli_format"
                    )

            # 6. Optional positional before required
            if positional:
                positional_sorted = sorted(positional, key=lambda x: x[0])
                found_optional = False
                for _, a in positional_sorted:
                    if not a.required:
                        found_optional = True
                    elif found_optional and a.required:
                        errors.append(
                            f"{s.directory}/{t.name}: required positional '{a.name}' "
                            f"after optional positional"
                        )

            # 7. enum only for string/integer
            for a in t.args:
                if a.enum and a.type not in ("string", "integer"):
                    warnings.append(
                        f"{s.directory}/{t.name}: enum on type '{a.type}' arg '{a.name}'"
                    )

            # 8. Default value type match
            for a in t.args:
                if a.has_default and a.default is not None:
                    type_ok = {
                        "string": lambda v: isinstance(v, str),
                        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                        "boolean": lambda v: isinstance(v, bool),
                    }.get(a.type, lambda v: True)
                    if not type_ok(a.default):
                        warnings.append(
                            f"{s.directory}/{t.name}: default for '{a.name}' "
                            f"type mismatch (expected {a.type}, got {type(a.default).__name__})"
                        )

            # 9. Mutually exclusive groups reference existing args
            arg_names = {a.name for a in t.args}
            for group in t.mutually_exclusive:
                for param in group:
                    if param not in arg_names:
                        errors.append(
                            f"{s.directory}/{t.name}: mutually_exclusive references "
                            f"nonexistent arg '{param}'"
                        )

    # Log warnings
    for w in warnings:
        logger.warning(f"DiskConfig warning: {w}")

    if errors:
        raise ValidationError("Disk configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


# ---------------------------------------------------------------------------
# DiskConfig main class
# ---------------------------------------------------------------------------

class DiskConfig:
    """Load and validate virtual disk YAML configuration.

    Accepts either a single config directory (str, for backward compatibility)
    or a list of directories. When a list is given, the first directory is the
    "bundle" directory (must exist); subsequent directories are user overlay
    directories (created on demand by the launcher; if missing they are skipped
    silently). Later directories override earlier ones by ``server_name``:
    if a user directory defines a server with the same ``server_name`` as one
    in the bundle, the user version wins (entire server replaced, not merged).

    Per-yaml parse failures are logged as warnings and skipped (do not block
    startup). Cross-file validation errors (duplicate directory names, builtin
    command conflicts, position gaps, etc.) remain strict and raise
    ``ValidationError`` to block startup.
    """

    def __init__(
        self,
        config_dirs: str | list[str] | os.PathLike,
        registry=None,
    ) -> None:
        # Normalize str → list[str] (backward compatibility for existing tests)
        if isinstance(config_dirs, (str, os.PathLike)):
            config_dirs = [str(config_dirs)]
        if not isinstance(config_dirs, (list, tuple)) or not config_dirs:
            raise ValueError(
                "config_dirs must be a non-empty list or str, got: "
                f"{type(config_dirs).__name__}"
            )

        # Resolve & filter directories: keep existing ones, skip missing user dirs
        resolved: list[Path] = []
        for raw in config_dirs:
            p = Path(raw)
            if p.is_dir():
                resolved.append(p)
            else:
                logger.warning(
                    "DiskConfig: directory does not exist, skipping: %s", p
                )

        # At least one directory must exist (bundle must exist)
        if not resolved:
            first = config_dirs[0] if config_dirs else None
            raise FileNotFoundError(
                f"Disk config directory not found: {first}"
            )

        self._config_dirs = resolved
        self._servers: dict[str, ServerConfig] = {}
        # dir_name → server_name; rebuilt at end so renames in user overlay
        # don't leave stale entries pointing at old server_name keys.
        self._directory_map: dict[str, str] = {}

        # Iterate directories in order; later dirs override by server_name.
        # Per-yaml errors (YAMLError, missing 'server' key, parse failure)
        # are logged as warnings and skipped — they do not block startup.
        for cfg_path in resolved:
            for yaml_file in sorted(cfg_path.glob("*.yaml")):
                # Legacy global config file (now removed from bundle). If a
                # user keeps one in ~/.niu/disk/, just skip it — the four
                # fields it used to define are dead config.
                if yaml_file.name == "disk.yaml":
                    logger.warning(
                        "DiskConfig: %s is no longer used (dead config), skipping.",
                        yaml_file,
                    )
                    continue
                try:
                    with open(yaml_file, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                except yaml.YAMLError as e:
                    logger.warning(
                        "DiskConfig: invalid YAML in %s, skipping: %s",
                        yaml_file.name, e,
                    )
                    continue
                except OSError as e:
                    logger.warning(
                        "DiskConfig: cannot read %s, skipping: %s",
                        yaml_file.name, e,
                    )
                    continue

                if not data or "server" not in data:
                    logger.warning(
                        "DiskConfig: %s has no 'server' key, skipping.",
                        yaml_file.name,
                    )
                    continue

                try:
                    server = _parse_server(data)
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning(
                        "DiskConfig: failed to parse %s, skipping: %s",
                        yaml_file.name, e,
                    )
                    continue

                # Override semantics: same server_name → later dir replaces earlier
                if server.server_name in self._servers:
                    logger.info(
                        "DiskConfig: server '%s' overridden by %s",
                        server.server_name, yaml_file,
                    )
                self._servers[server.server_name] = server

        # Rebuild directory_map from final _servers so renames don't leak.
        self._directory_map = {
            s.directory: s.server_name for s in self._servers.values()
        }

        # Cross-file validation (still strict — blocks startup on conflict).
        _validate_config(self._servers)

        # Cross-validate with registry (optional, warning only).
        if registry is not None:
            self._cross_validate_registry(registry)

    def _cross_validate_registry(self, registry) -> None:
        """Check YAML args against ToolRegistry input_schema, log warnings."""
        for server_name, server in self._servers.items():
            for tool_name, tool in server.tools.items():
                full_name = f"{server_name}/{tool_name}"
                schema = registry._schemas.get(full_name)
                if schema is None:
                    logger.warning(f"DiskConfig: {full_name} not found in ToolRegistry")
                    continue
                input_props = set(schema.get("input_schema", {}).get("properties", {}).keys())
                yaml_args = {a.name for a in tool.args}
                missing = input_props - yaml_args
                extra = yaml_args - input_props
                if missing:
                    logger.warning(f"DiskConfig: {full_name} YAML missing args: {missing}")
                if extra:
                    logger.warning(f"DiskConfig: {full_name} YAML has extra args: {extra}")

    # --- Public accessors ---

    @property
    def servers(self) -> dict[str, ServerConfig]:
        return self._servers

    @property
    def directory_map(self) -> dict[str, str]:
        return self._directory_map

    def get_server_by_dir(self, dir_name: str) -> ServerConfig | None:
        server_name = self._directory_map.get(dir_name)
        if server_name is None:
            return None
        return self._servers.get(server_name)

    def get_tool_config(self, dir_name: str, tool_name: str) -> ToolConfig | None:
        server = self.get_server_by_dir(dir_name)
        if server is None:
            return None
        return server.tools.get(tool_name)

    def list_visible_tools(self, dir_name: str) -> list[ToolConfig]:
        """List non-hidden tools in a directory."""
        server = self.get_server_by_dir(dir_name)
        if server is None:
            return []
        return [t for t in server.tools.values() if not t.hidden]

    def list_all_tools(self, dir_name: str) -> list[ToolConfig]:
        """List all tools (including hidden) in a directory."""
        server = self.get_server_by_dir(dir_name)
        if server is None:
            return []
        return list(server.tools.values())

    def list_directories(self) -> list[str]:
        """List all directory names."""
        return list(self._directory_map.keys())

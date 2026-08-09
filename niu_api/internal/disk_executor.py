"""Virtual Disk executor — Tool execution, argument validation, and error handling."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from niu_api.internal.disk_config import ArgConfig, DiskConfig, ToolConfig
from niu_api.internal.disk_errors import DiskErrors
from niu_api.internal.disk_parser import ParsedCommand

logger = logging.getLogger(__name__)


@dataclass
class ExecutorResult:
    """Structured return from DiskExecutor.execute().

    Separates error text from successful MCP tool results (which may be str, dict, or list).
    """
    is_error: bool
    value: Any  # Error text (str) on error, or raw MCP result on success


class DiskExecutor:
    """Execute tools with argument validation and error generation."""

    def __init__(self, config: DiskConfig, registry):
        self.config = config
        self.registry = registry
        self.errors = DiskErrors()
        self._error_count: dict[str, int] = {}  # tool_path → consecutive errors
        self._first_error: dict[str, bool] = {}  # tool_path → has shown first error

    def execute(self, parsed: ParsedCommand) -> ExecutorResult:
        """Execute a tool call. Returns ExecutorResult with is_error flag."""
        dir_name = parsed.dir_name
        tool_name = parsed.tool_name

        # Validate directory exists
        server = self.config.get_server_by_dir(dir_name)
        if server is None:
            available = sorted(self.config.directory_map.keys())
            return ExecutorResult(is_error=True, value=self.errors.path_not_found(f"/{dir_name}", available))

        # Validate tool exists
        tool = self.config.get_tool_config(dir_name, tool_name)
        if tool is None:
            available = [t.name for t in self.config.list_visible_tools(dir_name)]
            tool_path = f"/{dir_name}/{tool_name}"
            return ExecutorResult(is_error=True, value=self.errors.tool_not_found(dir_name, tool_name, available,
                                                                                  command=tool_path))

        tool_path = f"/{dir_name}/{tool_name}"

        # Build and validate kwargs from CLI args
        kwargs, error = self._build_kwargs(parsed, tool, tool_path)
        if error:
            return ExecutorResult(is_error=True, value=self._handle_param_error(tool_path, dir_name, tool, error))

        # Add defaults for args not provided
        kwargs = self._apply_defaults(kwargs, tool)

        # Call the real MCP tool
        full_name = f"{server.server_name}/{tool_name}"
        try:
            registry = self.registry
            if registry is None:
                from agent.tool_registry import get_registry
                registry = get_registry()
            func = registry.get(full_name)
            if func is None:
                return ExecutorResult(is_error=True, value=self.errors.execution_failure(tool_path, f"Tool '{full_name}' not found in registry"))
            from agent.handler import expand_path_args
            expand_path_args(kwargs)
            result = func(**kwargs)
            # Reset error tracking on success
            self._error_count.pop(tool_path, None)
            self._first_error.pop(tool_path, None)
            return ExecutorResult(is_error=False, value=result)
        except Exception as e:
            return ExecutorResult(is_error=True, value=self.errors.execution_failure(tool_path, str(e)))

    def _handle_param_error(self, tool_path: str, dir_name: str,
                            tool: ToolConfig, error: str) -> str:
        """Handle parameter validation error with escalation and tip."""
        # Track error count
        count = self._error_count.get(tool_path, 0) + 1
        self._error_count[tool_path] = count

        # E17: Escalation after 3 consecutive errors
        if count >= 3:
            return self.errors.repeated_errors(dir_name, tool)

        # Add first-error tip
        is_first = self._first_error.get(tool_path, True)
        self._first_error[tool_path] = False
        return self.errors.with_first_error_tip(error, tool_path, first_error=is_first)

    def _build_kwargs(self, parsed: ParsedCommand, tool: ToolConfig,
                      tool_path: str) -> tuple[dict[str, Any], str | None]:
        """Build MCP kwargs from parsed CLI args. Returns (kwargs, error_msg or None)."""
        kwargs: dict[str, Any] = {}
        provided_args: set[str] = set()

        # Positional args
        positional_args = [a for a in tool.args if a.position is not None]
        positional_args.sort(key=lambda a: a.position)

        # Check for too many positional args (E10)
        max_positional = len(positional_args)
        if len(parsed.positional_args) > max_positional:
            return {}, self.errors.too_many_args(tool_path, max_positional,
                                                 len(parsed.positional_args))

        # Map positional args
        for i, value in enumerate(parsed.positional_args):
            arg = positional_args[i]
            converted, error = self._convert_value(value, arg, tool_path)
            if error:
                return {}, error
            kwargs[arg.name] = converted
            provided_args.add(arg.name)

        # Check required positional args (E5)
        for arg in positional_args:
            if arg.required and arg.name not in provided_args:
                return {}, self.errors.missing_required_arg(
                    parsed.dir_name or "", tool, arg.name
                )

        # Flag args
        flag_lookup = {a.flag: a for a in tool.args if a.position is None}
        for flag_key, flag_value in parsed.flag_args.items():
            if flag_key not in flag_lookup:
                # Unknown flag (E7)
                available_flags = [a.flag for a in tool.args if a.position is None]
                return {}, self.errors.unknown_flag(
                    parsed.dir_name or "", tool, flag_key, available_flags
                )
            arg = flag_lookup[flag_key]

            if isinstance(flag_value, list):
                # Repeatable flag: --tag a --tag b → ["a", "b"]
                if arg.type != "array" or arg.cli_format != "repeatable":
                    return {}, self.errors.unknown_flag(
                        parsed.dir_name or "", tool, flag_key,
                        [a.flag for a in tool.args if a.position is None]
                    )
                converted_list = []
                for v in flag_value:
                    c, err = self._convert_value(str(v), arg, tool_path)
                    if err:
                        return {}, err
                    converted_list.extend(c if isinstance(c, list) else [c])
                kwargs[arg.name] = converted_list
            elif flag_value is True:
                # Boolean flag
                if arg.type != "boolean":
                    # Flag needs a value (E14)
                    return {}, self.errors.flag_missing_value(tool_path, flag_key)
                kwargs[arg.name] = True
            else:
                converted, error = self._convert_value(str(flag_value), arg, tool_path)
                if error:
                    return {}, error
                kwargs[arg.name] = converted
            provided_args.add(arg.name)

        # Check required flag args (E5)
        for arg in tool.args:
            if arg.required and arg.position is None and arg.name not in provided_args:
                return {}, self.errors.missing_required_arg(parsed.dir_name or "", tool, arg.name)

        # Check mutually exclusive (E18)
        if tool.mutually_exclusive:
            for group in tool.mutually_exclusive:
                provided_in_group = [p for p in group if p in provided_args]
                if len(provided_in_group) > 1:
                    return {}, self.errors.mutually_exclusive(
                        tool_path, provided_in_group[0], provided_in_group[1], group
                    )

        return kwargs, None

    def _convert_value(self, value: str, arg: ArgConfig,
                       tool_path: str) -> tuple[Any, str | None]:
        """Convert a CLI string value to the expected type. Returns (value, error)."""
        converted: Any = value
        if arg.type == "string":
            pass
        elif arg.type == "integer":
            try:
                converted = int(value)
            except ValueError:
                return None, self.errors.type_mismatch(tool_path, arg.flag or arg.name, "integer", value)
        elif arg.type == "number":
            try:
                converted = float(value)
            except ValueError:
                return None, self.errors.type_mismatch(tool_path, arg.flag or arg.name, "number", value)
        elif arg.type == "boolean":
            converted = value.lower() in ("true", "1", "yes")
        elif arg.type == "object":
            if arg.cli_format == "json":
                try:
                    converted = json.loads(value)
                except json.JSONDecodeError:
                    return None, f"{tool_path}: --{arg.flag} invalid JSON: {value}"
            elif arg.cli_format == "key=value":
                result = {}
                for pair in value.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        result[k.strip()] = v.strip()
                    else:
                        return None, f"{tool_path}: --{arg.flag} invalid key=value format"
                converted = result
            else:
                return None, f"{tool_path}: --{arg.flag} object arg requires cli_format (json or key=value)"
        elif arg.type == "array":
            if arg.cli_format == "json":
                try:
                    converted = json.loads(value)
                    if not isinstance(converted, list):
                        # 合法 JSON 标量（数字/布尔/null）对 array 参数也包成单元素数组
                        converted = [converted]
                except json.JSONDecodeError:
                    if not value or not value.strip():
                        # 空值保持报错（spec：空值报错不变），格式与 _convert_value 其他错误一致
                        return None, f"{tool_path}: --{arg.flag} invalid JSON array: {value}"
                    # 裸字符串容错：无法解析为 JSON 时包成单元素数组
                    converted = [value]
            elif arg.cli_format == "repeatable":
                converted = [value]
            else:
                return None, f"{tool_path}: --{arg.flag} array arg requires cli_format (json or repeatable)"
        else:
            return None, f"{tool_path}: unsupported arg type '{arg.type}'"

        # Enum validation (E8)
        if arg.enum and converted not in arg.enum:
            return None, self.errors.enum_error(tool_path, arg.flag or arg.name, converted, arg.enum)

        # Constraint validation (E9)
        if arg.constraints:
            if arg.type in ("integer", "number"):
                min_val = arg.constraints.get("minimum")
                max_val = arg.constraints.get("maximum")
                if min_val is not None and converted < min_val:
                    return None, self.errors.out_of_range(tool_path, arg.flag or arg.name, converted,
                                                          minimum=min_val, maximum=max_val)
                if max_val is not None and converted > max_val:
                    return None, self.errors.out_of_range(tool_path, arg.flag or arg.name, converted,
                                                          minimum=min_val, maximum=max_val)

        return converted, None

    def _apply_defaults(self, kwargs: dict[str, Any], tool: ToolConfig) -> dict[str, Any]:
        """Apply default values for args not provided by the user."""
        result = dict(kwargs)
        for arg in tool.args:
            if arg.name not in result and arg.has_default:
                result[arg.name] = arg.default
        return result

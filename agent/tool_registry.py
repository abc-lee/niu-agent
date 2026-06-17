"""
ToolRegistry - MCP工具注册中心

管理所有MCP工具的注册和调用，支持同进程直接调用。

设计目标：
- 启动时注册所有MCP服务器的工具
- 后续直接通过Python函数调用，无需stdio通信
- 保持与现有工具调用链路的兼容性

用法：
    from agent.tool_registry import get_registry

    # 注册服务器工具
    registry = get_registry()
    registry.register_server("memory-server", memory_module)

    # 获取工具函数
    tool_fn = registry.get("memory-server/user_memory_remember")

    # 获取工具schema列表（用于LLM）
    schemas = registry.get_schemas()
"""

from typing import Dict, Any, List, Optional, Callable
from loguru import logger


class ToolRegistry:
    """
    MCP工具注册中心

    管理所有MCP服务器的工具注册、获取和schema返回。
    """

    def __init__(self):
        """初始化工具注册表"""
        # 工具函数映射: "server/tool" -> callable
        self._tools: Dict[str, Callable] = {}

        # 工具schema映射: "server/tool" -> schema dict
        self._schemas: Dict[str, Dict[str, Any]] = {}

        # 服务器注册追踪: server_name -> list of tool names
        self._server_tools: Dict[str, List[str]] = {}

        # Agent LLM 回调函数，供内部 MCP Server 调用
        self._ask_agent = None  # callable(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str

        # 外部 MCP 工具映射: full_name -> (server_name, tool_name)
        self._external_tools: Dict[str, tuple[str, str]] = {}

        # MCPClientManager 实例，供外部工具调用
        self._mcp_client = None

    def register(
        self,
        name: str,
        func: Callable,
        schema: Dict[str, Any],
        visibility: str = "static",
    ) -> None:
        """Register a single tool directly (not from an MCP server module).

        Use this for built-in tools that wrap internal adapters rather than
        belonging to an MCP server.

        Args:
            name: Tool name (e.g. "lightrag-query").
            func: Callable that implements the tool.
            schema: Tool schema dict with keys: name, description, input_schema.
            visibility: "static" or "hidden". Defaults to "static".
        """
        self._tools[name] = func
        normalized_schema = {
            "name": name,
            "description": schema.get("description", ""),
            "input_schema": schema.get("input_schema", schema.get("inputSchema", {})),
            "visibility": visibility,
        }
        self._schemas[name] = normalized_schema
        # Track under a virtual "__builtin__" server so list_tools / clear work
        self._server_tools.setdefault("__builtin__", [])
        if name not in self._server_tools["__builtin__"]:
            self._server_tools["__builtin__"].append(name)
        logger.info(f"Registered built-in tool: {name}")

    def register_server(self, server_name: str, module, visibility_map: Optional[dict] = None) -> bool:
        """
        注册MCP服务器的所有工具

        Args:
            server_name: 服务器名称（如 "photo-server"）
            module: Python模块对象，必须提供get_tool_schemas()函数
            visibility_map: 工具可见性映射，格式 {"tool_name": {"visibility": "static"/"hidden"}}

        Returns:
            True if registration succeeded, False otherwise
        """
        # 检查模块是否有get_tool_schemas函数
        if not hasattr(module, 'get_tool_schemas'):
            logger.warning(f"Module {module} does not have get_tool_schemas function")
            return False

        try:
            # 获取工具schema列表
            tool_schemas = module.get_tool_schemas()
            if not tool_schemas:
                logger.warning(f"No tools returned from {server_name}")
                return True  # Empty list is still valid registration

            # 如果之前注册过该服务器，先清理
            if server_name in self._server_tools:
                old_tools = self._server_tools[server_name]
                for tool_name in old_tools:
                    full_name = f"{server_name}/{tool_name}"
                    self._tools.pop(full_name, None)
                    self._schemas.pop(full_name, None)
                logger.debug(f"Cleared {len(old_tools)} old tools from {server_name}")

            # 注册新工具
            registered_tools = []
            for schema in tool_schemas:
                tool_name = schema.get("name")
                if not tool_name:
                    logger.warning(f"Tool schema missing 'name': {schema}")
                    continue

                full_name = f"{server_name}/{tool_name}"

                # 确定 visibility
                tool_vis = "hidden"  # 默认值
                if visibility_map and tool_name in visibility_map:
                    tool_vis = visibility_map[tool_name].get("visibility", "hidden")

                # 存储schema（确保使用input_schema格式）
                normalized_schema = {
                    "name": full_name,
                    "description": schema.get("description", ""),
                    "input_schema": schema.get("input_schema", schema.get("inputSchema", {})),
                    "visibility": tool_vis
                }
                self._schemas[full_name] = normalized_schema

                # 尝试获取工具函数
                # 模式1：模块级函数
                # 模式2：get_tool_function() 方法
                # 模式3：call_tool() 处理器（MCP 标准模式）
                tool_fn = None
                if hasattr(module, tool_name):
                    raw_fn = getattr(module, tool_name)
                    # Wrap direct function with argument filtering to prevent
                    # TypeError from LLM-sent extra arguments
                    import inspect as _inspect
                    sig = None
                    valid_params = None
                    try:
                        sig = _inspect.signature(raw_fn)
                        valid_params = set(sig.parameters.keys())
                    except (ValueError, TypeError):
                        pass

                    if valid_params is not None:
                        # If the function accepts **kwargs (VAR_KEYWORD),
                        # it already handles arbitrary arguments — skip filtering.
                        has_var_keyword = any(
                            p.kind == _inspect.Parameter.VAR_KEYWORD
                            for p in sig.parameters.values()
                        )
                        if has_var_keyword:
                            tool_fn = raw_fn
                        else:
                            def _make_filtered_fn(fn, params):
                                def filtered_fn(**kwargs):
                                    filtered = {k: v for k, v in kwargs.items() if k in params}
                                    return fn(**filtered)
                                return filtered_fn
                            tool_fn = _make_filtered_fn(raw_fn, valid_params)
                    else:
                        tool_fn = raw_fn
                elif hasattr(module, 'get_tool_function'):
                    tool_fn = module.get_tool_function(tool_name)

                # 模式3：如果没找到单独的函数，包装 call_tool()
                if tool_fn is None and hasattr(module, 'call_tool'):
                    # 创建一个包装函数来调用 call_tool
                    def make_wrapper(server_module, name):
                        def wrapper(**kwargs):
                            return server_module.call_tool(name, kwargs)
                        return wrapper

                    tool_fn = make_wrapper(module, tool_name)
                    logger.debug(f"Wrapped call_tool() for {full_name}")

                if tool_fn and callable(tool_fn):
                    self._tools[full_name] = tool_fn
                    logger.debug(f"Registered tool: {full_name}")
                else:
                    logger.warning(f"Tool function not found for: {full_name}")

                registered_tools.append(tool_name)

            self._server_tools[server_name] = registered_tools
            logger.info(f"Registered {len(registered_tools)} tools from {server_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to register server {server_name}: {e}")
            return False

    def get(self, tool_name: str) -> Optional[Callable]:
        """获取工具函数——内部返回函数引用，外部返回 Client 调用包装器

        Args:
            tool_name: 完整工具名（如 "server-name/tool-name"）

        Returns:
            工具函数，如果不存在则返回None
        """
        if tool_name in self._tools:
            return self._tools[tool_name]
        if tool_name in self._external_tools:
            server_name, tool_name_raw = self._external_tools[tool_name]
            if self._mcp_client is None:
                return None

            def wrapper(**kwargs):
                return self._mcp_client.call_tool_sync(server_name, tool_name_raw, kwargs)

            return wrapper
        return None

    def get_schemas(self) -> List[Dict[str, Any]]:
        """
        返回工具schema列表（用于LLM）

        Returns:
            所有已注册工具的schema列表
        """
        return list(self._schemas.values())

    def get_all_schemas(self) -> Dict[str, Dict[str, Any]]:
        """Return dict of all tool schemas keyed by full tool name.

        Useful when callers need both the tool name and its schema,
        e.g. for iterating server/tool pairs to inject into LightRAG.

        Returns:
            Dict mapping full tool name (e.g. "server/tool") to schema dict.
        """
        return dict(self._schemas)

    def list_tools(self) -> List[str]:
        """列出所有已注册的工具名称（内部 + 外部）

        Returns:
            工具名称列表
        """
        return list(self._tools.keys()) + list(self._external_tools.keys())

    def has_tool(self, tool_name: str) -> bool:
        """检查工具是否已注册（内部或外部）

        Args:
            tool_name: 完整工具名

        Returns:
            True if tool is registered
        """
        return tool_name in self._tools or tool_name in self._external_tools

    def get_static_tools(self) -> List[str]:
        """
        返回所有 visibility=static 的工具名列表

        替代 runner.py 中硬编码的 BASE_MCP_TOOLS
        """
        return [name for name, schema in self._schemas.items() if schema.get("visibility") == "static"]

    def set_ask_agent(self, fn):
        """注入 Agent LLM 回调函数，供内部 MCP Server 调用"""
        self._ask_agent = fn

    def set_mcp_client(self, client):
        """注入 MCPClientManager 实例，供外部工具调用"""
        self._mcp_client = client

    def ask_agent(self, prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
        """请求 Agent LLM 生成回答。返回文本或 None（如果不可用）"""
        if self._ask_agent is None:
            return None
        try:
            import inspect as _inspect
            sig = _inspect.signature(self._ask_agent)
            valid_params = set(sig.parameters.keys())
            kwargs = {k: v for k, v in {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "max_tokens": max_tokens,
            }.items() if k in valid_params}
            return self._ask_agent(**kwargs)
        except Exception:
            return None

    def clear(self):
        """清空所有注册的工具"""
        self._tools.clear()
        self._schemas.clear()
        self._server_tools.clear()
        self._external_tools.clear()
        self._mcp_client = None
        logger.info("ToolRegistry cleared")


# ============================================================================
# 全局registry实例管理
# ============================================================================

_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """
    获取全局ToolRegistry实例

    如果实例不存在，会自动创建一个新的实例。

    Returns:
        ToolRegistry实例
    """
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        logger.info("Created global ToolRegistry instance")
    return _registry


def set_registry(registry: ToolRegistry):
    """
    设置全局ToolRegistry实例

    用于测试或需要自定义registry的场景。

    Args:
        registry: 要设置的ToolRegistry实例
    """
    global _registry
    _registry = registry
    logger.info("Set global ToolRegistry instance")


def reset_registry():
    """
    重置全局ToolRegistry实例

    主要用于测试场景。
    """
    global _registry
    if _registry is not None:
        _registry.clear()
    _registry = None
    logger.info("Reset global ToolRegistry instance")

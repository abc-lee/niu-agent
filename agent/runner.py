"""
Niu Agent Runner

简化的 Agent 入口，直接使用 GenericAgent 组件。
Disk mode: MCP 工具通过虚拟磁盘 disk() 发现和调用，
Skills/知识通过 LightRAG 动态注入提示词。
"""

import json
import os
import queue as _queue_module
import re
import sys
import io
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from loguru import logger

from niu_api.internal.lightrag_adapter import _LIGHTRAG_ERROR_MARKERS


# --- Stop flag mechanism ---
_stop_requested = threading.Event()


def request_stop():
    """Set the stop flag — Agent loops will check and exit."""
    _stop_requested.set()


def clear_stop():
    """Clear the stop flag — called when Agent loop exits and at conversation start."""
    _stop_requested.clear()


def is_stop_requested() -> bool:
    """Check if stop has been requested."""
    return _stop_requested.is_set()


# --- Supplement queue (见缝插针) ---
_supplement_queue = _queue_module.Queue()  # 无限长度，永不阻塞


def enqueue_supplement(content: str):
    """将用户在 Agent 运行期间发送的补充消息放入队列。"""
    _supplement_queue.put(content)


def drain_supplements() -> list[str]:
    """取出所有补充消息（非阻塞，无竞态）。"""
    msgs = []
    while True:
        try:
            msgs.append(_supplement_queue.get_nowait())
        except _queue_module.Empty:
            break
    return msgs


def drain_supplement() -> str | None:
    """取出所有补充消息，格式化为单条字符串。

    - 无消息返回 None
    - 单条返回原文
    - 多条合并为 "[补充] 消息1\\n[补充] 消息2"
    """
    msgs = drain_supplements()
    if not msgs:
        return None
    if len(msgs) == 1:
        return msgs[0]
    return "\n".join(f"[补充] {m}" for m in msgs)


def _sanitize_memory_content(content: str) -> str:
    """Sanitize user memory content before injecting into system prompt.
    Prevents prompt injection by removing newlines and sentinel markers."""
    if content is None:
        return ""
    if not isinstance(content, str):
        content = str(content)
    # Remove newlines to prevent multi-line injection
    content = content.replace("\n", " ").replace("\r", " ")
    # Remove sentinel markers to prevent section boundary spoofing
    content = content.replace("<!--USER_MEMORY_START-->", "").replace("<!--USER_MEMORY_END-->", "")
    # Remove markdown headers to prevent section injection
    content = re.sub(r"^#{1,6}\s*", "", content, flags=re.MULTILINE)
    # Truncate to 300 chars as hard limit
    if len(content) > 300:
        content = content[:300] + "..."
    return content.strip()


def _strip_lightrag_error_lines(text: str) -> str:
    """Remove lines containing LightRAG fail_response markers from text.

    Filters out any line that contains LightRAG's canned error markers
    (e.g. "not able to provide" or "[no-context]"), which indicate the
    query returned no results.  These are NOT LLM-generated content and
    must not appear in the system prompt.

    Args:
        text: Multi-line text that may contain LightRAG error lines.

    Returns:
        The text with error lines removed.  Returns empty string if all
        lines are error lines or the result is whitespace-only.
    """
    if not text or not isinstance(text, str):
        return ""
    lower = text.lower()
    # Fast path: if no markers present, return as-is
    if not any(marker in lower for marker in _LIGHTRAG_ERROR_MARKERS):
        return text
    # Filter out lines containing any marker
    filtered_lines = [
        line for line in text.split("\n")
        if not any(marker in line.lower() for marker in _LIGHTRAG_ERROR_MARKERS)
    ]
    result = "\n".join(filtered_lines).strip()
    return result


def _render_permanent_section(permanent: list) -> str:
    """Render permanent memory items into a system prompt section.
    Shared by _load_memory_for_prompt and _refresh_user_memories."""
    if not permanent:
        return ""
    lines = ["### [用户长期记忆]"]
    # Normalize old string format to dict, default missing type to "memory"
    normalized = []
    for item in permanent:
        if isinstance(item, str):
            normalized.append({"type": "memory", "content": item})
        elif isinstance(item, dict):
            normalized.append({**item, "type": item.get("type", "memory")})
    # Task items first (skip empty content — cleared task slot)
    task_items = [item for item in normalized if item.get("type") == "task" and item.get("content")]
    memory_items = [item for item in normalized if item.get("type") == "memory"]
    if task_items:
        lines.append(f"📋 当前任务：{_sanitize_memory_content(task_items[0].get('content', ''))}")
    if memory_items:
        lines.append("以下内容用户特别强调，必须始终遵守：")
        for i, item in enumerate(memory_items, 1):
            lines.append(f"{i}. {_sanitize_memory_content(item.get('content', str(item)))}")
    lines.append(f"（共{len(normalized)}/5条，使用 memory-server/user_memory_remember 添加，memory-server/user_memory_forget 删除）")
    return "<!--USER_MEMORY_START-->\n" + "\n".join(lines) + "\n<!--USER_MEMORY_END-->"

# 修复Windows控制台编码问题
if sys.platform == 'win32':
    # 确保stderr使用UTF-8编码
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from .generic.agent_loop import agent_runner_loop, StreamEvent
from .generic.llmcore import ToolClient
from .handler import NiuHandler
from .injector.sync import get_skill_sync
from agent.tool_registry import get_registry


def get_system_prompt() -> str:
    """获取系统提示词"""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. 读取 niu.md 配置（已合并原 sys_prompt.txt 内容）
    sys_prompt = ""
    niu_md_path = os.path.join(script_dir, "..", "config", "agents", "niu.md")
    if os.path.exists(niu_md_path):
        with open(niu_md_path, "r", encoding="utf-8") as f:
            content = f.read()
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    # 注入 front matter 中的 description 字段
                    try:
                        import yaml as _yaml
                        config = _yaml.safe_load(parts[1])
                        if config and config.get("description"):
                            sys_prompt = config["description"].strip() + "\n\n"
                    except Exception:
                        pass
                    sys_prompt += parts[2].strip()
            else:
                sys_prompt = content

    if not sys_prompt:
        sys_prompt = "# Role: Niu Agent\nYou are a helpful assistant with file and code access."

    # 2. 注入 memory.json 中的身份设定和用户偏好
    memory_section = _load_memory_for_prompt()
    if memory_section:
        sys_prompt += "\n\n" + memory_section

    # 3. 添加当前时间
    now = datetime.now()
    sys_prompt += f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

    return sys_prompt


def _load_memory_for_prompt() -> str:
    """从 memory.json 加载身份设定和用户偏好，格式化为提示词"""
    memory_path = Path.home() / ".niu" / "memory.json"
    if not memory_path.exists():
        return ""

    try:
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    parts = []

    # 身份设定
    identity = memory.get("identity", {})
    if identity:
        name = identity.get("name", "妞妞")
        personality = identity.get("personality", [])
        greeting_style = identity.get("greetingStyle", "")

        identity_str = f"## 身份设定\n\n你的名字是 **{name}**。"
        if personality:
            identity_str += f"\n性格特质：{'、'.join(personality)}。"
        if greeting_style:
            identity_str += f"\n问候风格：{greeting_style}。"
        parts.append(identity_str)

    # 工作目录
    workspace = memory.get("workspace", {})
    ws_path = workspace.get("path", "")
    if ws_path and not str(ws_path).startswith("请询问"):
        parts.append(f"## 工作目录\n\n知识库目录：{ws_path}")

    # 用户信息
    user = memory.get("user", {})
    user_lines = []
    if user.get("name") and not str(user["name"]).startswith("请询问"):
        user_lines.append(f"真实姓名：{user['name']}")
    if user.get("nickname") and not str(user["nickname"]).startswith("请询问"):
        user_lines.append(f"称呼：{user['nickname']}")
    if user.get("occupation") and not str(user["occupation"]).startswith("请询问"):
        user_lines.append(f"职业：{user['occupation']}")
    if user.get("organization") and not str(user["organization"]).startswith("请询问"):
        user_lines.append(f"工作单位：{user['organization']}")
    if user_lines:
        user_str = "## 用户信息\n\n" + "\n".join(user_lines)
        parts.append(user_str)

    # 用户长期记忆（驻留在 system prompt，最多5条，每条≤200 token）
    permanent = memory.get("permanent", [])
    perm_str = _render_permanent_section(permanent)
    if perm_str:
        parts.append(perm_str)

    # 首次使用引导（firstRun）
    # 如果 memory.json 中存在 firstRun 字段，说明用户尚未完成初始设置
    # AI 应主动询问用户工作目录，并帮助用户完成 memory.json 的写入
    if memory.get("firstRun"):
        parts.append(
            "## 首次使用\n\n"
            "用户尚未完成初始设置。你需要主动询问用户工作目录路径。\n"
            "请说：\"嗨！我是妞妞。为了帮你管理知识，请告诉我你的工作目录想放在哪里？\"\n\n"
            "用户回答路径后，你需要用 bash 工具完成以下操作：\n"
            "1. 创建目录（如果不存在）\n"
            "2. 写入 ~/.niu/memory.json：设置 workspace.path，将 firstRun 设为 false\n\n"
            "完成后，下次对话不再出现此提示。"
        )

    return "\n\n".join(parts)


def get_tools_schema() -> list:
    """获取工具 Schema（从 JSON 文件加载 + 注册子 Agent 工具）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(script_dir, "generic", "assets", "tools_schema.json")

    tools = []
    if os.path.exists(schema_path):
        with open(schema_path, "r", encoding="utf-8") as f:
            tools = json.load(f)

    # 注册子 Agent 工具（从 niu.md 的 sub agents 字段动态生成）
    from .subagent import get_subagent_config
    try:
        niu_config = get_subagent_config("niu")
        sub_agents = niu_config.get("sub agents", [])
    except Exception as e:
        logger.warning(f"Failed to load niu.md sub agents config: {e}")
        sub_agents = []

    for agent_name in sub_agents:
        task_desc = "描述要委托给子Agent执行的任务"  # 默认值
        try:
            agent_config = get_subagent_config(agent_name)
            desc = agent_config.get("description", f"子 Agent: {agent_name}")
            task_desc = agent_config.get("taskDescription", task_desc)
        except Exception as e:
            logger.warning(f"Failed to load sub-agent '{agent_name}' config: {e}")
            desc = f"子 Agent: {agent_name}"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"chat-with-{agent_name}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": task_desc,
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )

    return tools


def create_client(config: Dict[str, Any]):
    """创建 LLM 客户端（统一使用LiteLLM）"""
    cfg = {
        "apikey": config.get("apikey") or config.get("api_key", ""),
        "apibase": config.get("apibase") or config.get("api_base", ""),
        "model": config.get("model", ""),
        "api_type": config.get("type", "openai"),
    }
    if "temperature" in config and config["temperature"] is not None:
        cfg["temperature"] = config["temperature"]

    from .generic.litellm_adapter import create_litellm_client
    logger.info(f"Using LiteLLM adapter for model: {cfg['model']}")
    return create_litellm_client(cfg)


def format_resources_for_prompt(results: list, title: str = "相关资源") -> str:
    """
    格式化资源为提示词注入格式

    格式：
    ### [相关资源]
    1. **name** (分数: 87)
       完整内容...
    """
    if not results:
        return ""

    lines = [f"\n\n### [{title}]"]
    for i, r in enumerate(results, 1):
        score_pct = int(r.score * 100)
        name = r.metadata.get("name", "")

        if name:
            lines.append(f"{i}. **{name}** (分数: {score_pct})")
            # Skills 等其他类型，注入 L1 摘要 + 文件路径（指针）
            lines.append(f"   {r.content}")
            source = r.metadata.get("source", "")
            if source:
                lines.append(f"   文件路径: {source}")
        else:
            lines.append(f"{i}. {r.content} (分数: {score_pct})")

    return "\n".join(lines)



class NiuRunner:
    """
    Niu Agent Runner

    简化的 Agent 运行器，直接使用 GenericAgent 组件。
    集成动态注入：Skills 按语义注入提示词，MCP 工具按分数动态注入 tools_schema。
    """

    def __init__(self, llm_config: Dict[str, Any], mcp_client=None):
        # 从 niu.md front matter 读取 temperature，覆盖到 llm_config
        from .subagent import get_subagent_config
        niu_config = get_subagent_config("niu")
        if niu_config.get("temperature") is not None:
            llm_config = {**llm_config, "temperature": niu_config["temperature"]}

        self.llm_config = llm_config
        self.mcp_client = mcp_client
        self.client = create_client(llm_config)
        project_root = os.path.dirname(os.path.dirname(__file__))
        self.handler = NiuHandler(cwd=project_root, mcp_client=mcp_client)
        self.base_system_prompt = get_system_prompt()
        self.base_tools_schema = get_tools_schema()

        # 启动 Skills 后台同步
        get_skill_sync(auto_start=True)

        # MCP 工具列表（启动时加载，缓存）
        self._mcp_tools_schema: list = []

        # DiskEngine（虚拟磁盘命令引擎）
        from niu_api.internal.disk_engine import DiskEngine
        disk_config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "disk")
        self.disk_engine = DiskEngine(disk_config_dir, registry=None)
        self.handler = NiuHandler(cwd=project_root, mcp_client=mcp_client, disk_engine=self.disk_engine)

        # Inject disk description into base system prompt
        disk_desc = self._build_disk_description()
        if disk_desc:
            self.base_system_prompt += disk_desc

        # 用户记忆脏标记（remember/forget 工具调用后 set）
        self._memory_dirty = threading.Event()

        # Brain context injector chain (lazy-cached, created once per runner)
        self._brain_adapter = None      # LightRAGAdapter
        self._brain_ingester = None     # LightRAGIngester
        self._brain_region_mgr = None   # RegionManager
        self._brain_injector = None     # BrainContextInjector
        self._cached_activation_mgr = None  # RegionActivationManager (for cache invalidation)

        # 注入 ask_agent callback（供内部 MCP Server 调用 LLM）
        _registry = get_registry()
        _registry.set_ask_agent(self._make_ask_agent_callback())

        # 初始化 MCPClientManager 并连接外部 MCP 服务器
        from agent.mcp_client import MCPClientManager, make_sampling_callback
        self._ext_mcp_client = MCPClientManager(sampling_callback=make_sampling_callback())
        _registry.set_mcp_client(self._ext_mcp_client)
        # 注意：_connect_external_servers 是 async，需要在 async 上下文中调用
        # 这里暂时不调用，由 lifespan 的 startup 事件触发

    def _make_ask_agent_callback(self):
        """创建 ask_agent 回调，调用当前 Agent 的 LLM"""
        def ask_agent(prompt: str, system_prompt: str = "", max_tokens: int = 500) -> str | None:
            try:
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})
                response = self.client.chat.completions.create(
                    model=self.llm_config["model"],
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"ask_agent failed: {e}")
                return None
        return ask_agent

    async def _connect_external_servers(self):
        """连接外部 MCP 服务器（stdio/HTTP 模式）"""
        try:
            from agent.mcp_loader import load_external_servers
            await load_external_servers(self._ext_mcp_client)
        except Exception as e:
            logger.warning(f"Failed to connect external MCP servers: {e}")

    async def startup(self):
        """异步启动，连接外部 MCP 服务器。由 lifespan startup 事件调用。"""
        await self._connect_external_servers()

    def set_mcp_tools_schema(self, tools: list):
        """Set MCP tool schemas — in disk mode, only inject disk() schema.

        All MCP tools are visibility=hidden and accessed via disk().
        The tools list is stored for potential future lookups.
        Also passes registry to DiskEngine for cross-validation and tool lookup.
        """
        self._mcp_tools_schema = tools  # Store for schema lookups
        # Pass registry to DiskEngine for cross-validation and tool lookup
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            self.disk_engine.executor.registry = registry
            self.disk_engine._registry = registry
            self.disk_engine.config._cross_validate_registry(registry)
        except Exception as e:
            logger.warning(f"DiskEngine registry binding failed: {e}")
        logger.info(f"Loaded {len(tools)} MCP tools (all hidden, accessed via disk)")

    def _extract_context_from_messages(self, messages: list) -> str:
        """
        从 agent_runner_loop 的 messages 列表提取上下文。

        和 _extract_context_from_history 保持一致：
        - 只取 user/assistant 角色的 content（短截断）
        - 不取 tool 角色内容（工具返回太长，干扰向量检索）
        - 从 assistant 的 tool_calls 提取工具名（关键：让向量检索匹配同组工具）

        Args:
            messages: agent_runner_loop 的消息列表

        Returns:
            提取的上下文字符串
        """
        context_parts = []

        # 取最近3条消息（严格3条，不区分轮次）
        recent = messages[-3:] if len(messages) > 3 else messages

        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user" and content:
                # next_prompt（"工具调用成功。请向用户简洁汇报结果：{...}"）是 user 角色
                # 包含大量工具返回 JSON，只取前 50 字符
                if content.startswith("工具调用成功") or content.startswith("Tool call succeeded"):
                    context_parts.append(content[:50])
                else:
                    context_parts.append(content[:80])
            elif role == "assistant" and content:
                context_parts.append(content[:80])

            # 从 assistant 的 tool_calls 中提取工具名
            # 3条消息中assistant最多出现2次（第1、3条），每次最多1个工具名，共最多2个
            if role == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    if name:
                        context_parts.append(name)
                        break  # 每条assistant消息只取1个工具名

        return " ".join(context_parts) if context_parts else ""

    def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
        """每轮循环结束后刷新动态注入（skills/knowledge only, no MCP schema refresh)."""
        # Refresh user memories if dirty
        self._refresh_user_memories(messages)

        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # Extract context and re-inject skills/knowledge
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # Update system_prompt
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = self.base_system_prompt + injection

        # No schema refresh — tools_schema stays base + disk
        return tools_schema

    def _get_brain_injector(self):
        """Get or create the cached brain context injector chain.

        All four instances (LightRAGAdapter, LightRAGIngester,
        RegionManager, BrainContextInjector) are lightweight wrappers
        with no expensive initialization, but creating them every turn
        is unnecessary. Cached as instance variables on the runner.

        Includes cache invalidation: if the adapter's underlying LightRAG
        instance has been reset (e.g. after re-initialization), or if
        the activation_mgr singleton was replaced by region_sync, the
        cached wrappers are stale and must be recreated.

        Returns None if activation_mgr is not available (brain tools
        not initialized), matching the original guard condition.
        """
        from agent.brain_tools import get_activation_mgr

        # Invalidate cache if the adapter's LightRAG instance is gone
        # OR if the activation_mgr singleton was replaced by region_sync
        if self._brain_adapter is not None:
            try:
                rag = self._brain_adapter._get_rag()
                current_mgr = get_activation_mgr()
                if rag is None or current_mgr is not self._cached_activation_mgr:
                    self._brain_adapter = None
                    self._brain_ingester = None
                    self._brain_region_mgr = None
                    self._brain_injector = None
                    self._cached_activation_mgr = None
            except Exception:
                self._brain_adapter = None
                self._brain_ingester = None
                self._brain_region_mgr = None
                self._brain_injector = None
                self._cached_activation_mgr = None

        if self._brain_injector is None:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
            from niu_api.internal.region_manager import RegionManager
            from niu_api.internal.region_injector import BrainContextInjector

            self._brain_adapter = LightRAGAdapter()
            self._brain_ingester = LightRAGIngester()
            _activation_mgr = get_activation_mgr()
            if self._brain_adapter._get_rag() is None or _activation_mgr is None:
                # If activation_mgr is None, try forcing a RegionSync once
                if _activation_mgr is None and self._brain_adapter._get_rag() is not None:
                    try:
                        from agent.injector.region_sync import get_region_sync
                        logger.info("[BrainInjector] activation_mgr is None, forcing RegionSync.run_sync()")
                        get_region_sync().run_sync()
                        _activation_mgr = get_activation_mgr()
                    except Exception as e:
                        logger.error("[BrainInjector] Forced RegionSync failed: %s", e)
                # Re-check after forced sync attempt
                if self._brain_adapter._get_rag() is None or _activation_mgr is None:
                    if _activation_mgr is None:
                        logger.error("[BrainInjector] activation_mgr still None after forced sync, brain context disabled")
                    # LightRAG instance not available or activation mgr missing — invalidate cache
                    self._brain_adapter = None
                    self._brain_ingester = None
                    self._brain_region_mgr = None
                    self._brain_injector = None
                    self._cached_activation_mgr = None
                    return None
            self._cached_activation_mgr = _activation_mgr
            self._brain_region_mgr = RegionManager(self._brain_adapter, self._brain_ingester)
            self._brain_injector = BrainContextInjector(
                adapter=self._brain_adapter,
                activation_mgr=_activation_mgr,
                region_mgr=self._brain_region_mgr,
            )
        return self._brain_injector

    def _refresh_user_memories(self, messages: list):
        """Refresh the ### [用户长期记忆] section in system prompt if dirty"""
        if not self._memory_dirty.is_set():
            return
        self._memory_dirty.clear()

        # Read current permanent memories (use lock to avoid reading partial write)
        memory_path = Path.home() / ".niu" / "memory.json"
        try:
            from niu_memory_server import _memory_file_lock
            with _memory_file_lock:
                data = json.loads(memory_path.read_text(encoding="utf-8"))
                permanent = data.get("permanent", [])
                if not isinstance(permanent, list):
                    permanent = []
        except Exception:
            return

        # Use shared renderer (handles normalization, sanitization, empty task skip)
        new_section = _render_permanent_section(permanent)

        SECTION_START = "<!--USER_MEMORY_START-->"
        SECTION_END = "<!--USER_MEMORY_END-->"
        pattern = re.escape(SECTION_START) + r".*?" + re.escape(SECTION_END)

        # Update base_system_prompt so _on_turn_end's overwrite uses fresh memory
        base = self.base_system_prompt
        if re.search(pattern, base, re.DOTALL):
            if new_section:
                self.base_system_prompt = re.sub(pattern, new_section, base, flags=re.DOTALL)
            else:
                self.base_system_prompt = re.sub(r'\n*' + pattern + r'\n*', '', base, flags=re.DOTALL)
        elif new_section:
            self.base_system_prompt = base + "\n\n" + new_section

    def _extract_context_from_history(self, history: Optional[list], user_input: str) -> str:
        """
        从消息历史中提取上下文用于工具检索

        Args:
            history: 消息历史 [{"role": "user/assistant", "content": str}, ...]
            user_input: 当前用户输入

        Returns:
            提取的上下文字符串
        """
        if not history:
            return user_input

        # 提取最近3条消息（严格3条，不区分轮次）
        recent_messages = history[-3:] if len(history) > 3 else history

        # 拼接内容
        context_parts = []
        for msg in recent_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if content and role in ("user", "assistant"):
                # 截断过长的内容（80字符，保持向量匹配精度）
                if len(content) > 80:
                    content = content[:80] + "..."
                context_parts.append(f"{role}: {content}")

        # 添加当前用户输入
        context_parts.append(f"user: {user_input}")

        return "\n".join(context_parts)

    # ============== LightRAG Helper Methods ==============

    # 黑名单：这些实体类型/名称不应注入到主Agent system prompt
    _INJECT_ENTITY_TYPE_BLACKLIST = {"mcp_tool", "tool"}
    _INJECT_ENTITY_NAME_BLACKLIST = {
        # 源码文件名 — 内部实现细节，对Agent对话无帮助
        "agent_loop.py", "handler.py", "tool_registry.py",
        # 内部架构概念 — system prompt 硬编码已覆盖
        "主Agent", "context-manager", "chat_idle事件",
        # 子Agent工具名 — tools description 已覆盖
        "chat-with-file-processor", "chat-with-event-manager", "chat-with-journal-agent",
    }

    def _format_lightrag_entities_for_prompt(
        self, entities: list[dict], title: str, seen_names: set[str],
    ) -> tuple[str, set[str]]:
        """Format LightRAG entity dicts for prompt injection with blacklist filtering."""
        if not entities:
            return "", seen_names

        is_skill_section = title == "相关技能"
        lines = [f"\n\n### [{title}]"]
        added = 0
        for entity in entities:
            entity_name = entity.get("entity_name", "")
            display_name = entity_name

            # 过滤黑名单实体类型（如 mcp_tool/tool — 主Agent通过 disk 发现工具）
            # 注意：LightRAG 返回的 entity_type 可能是 title case（如 "Tool"），需 .lower()
            entity_type = entity.get("entity_type", "").lower()
            if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                logger.debug(f"[Inject] Skipping blacklisted type '{entity_type}': {display_name}")
                continue

            # 过滤黑名单实体名（源码文件名、硬编码已覆盖的架构概念）
            if display_name in self._INJECT_ENTITY_NAME_BLACKLIST:
                logger.debug(f"[Inject] Skipping blacklisted name: {display_name}")
                continue

            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            description = entity.get("description", "").replace("<SEP>", "\n")
            if description:
                lines.append(f"{added + 1}. **{display_name}**")
                lines.append(f"   {description}")
            else:
                lines.append(f"{added + 1}. **{display_name}**")
            if is_skill_section:
                lines.append(f"   路径: ~/.niu/skills/{display_name}.md")
            added += 1

        if added == 0:
            return "", seen_names
        return "\n".join(lines), seen_names

    # ============== Disk Description ==============

    def _build_disk_description(self) -> str:
        """Build disk tool description with dynamic directory listing for system prompt."""
        try:
            servers = self.disk_engine.config.servers
        except Exception:
            return ""

        dir_lines = []
        for server in servers.values():
            dir_lines.append(f"  /{server.directory:<10} — {server.description}")

        return (
            "\n\n### [虚拟磁盘工具]\n"
            "你有一个虚拟磁盘工具 disk(command)，可以用 Unix 命令探索和调用所有 MCP 工具。这是对 MCP 工具的虚拟化封装，不是系统磁盘，不能用于访问本地文件系统。\n\n"
            "命令:\n"
            "  ls /                列出所有目录\n"
            "  ls /<dir>           列出目录下的工具\n"
            "  cat /<dir>/readme.txt  查看目录说明（推荐先看这个再执行）\n"
            "  cat /<dir>/<tool>   查看工具详细用法\n"
            "  /<dir>/<tool> <args>  执行工具（位置参数直接写，不加 key=）\n\n"
            "示例:\n"
            "  cat /browser/readme.txt           先看 browser 目录说明\n"
            "  /browser/browser_navigate https://example.com  打开网页\n"
            "  /lightrag/lightrag_query 什么是知识图谱 --mode hybrid\n\n"
            "当前磁盘目录:\n"
            + "\n".join(dir_lines)
        )

    # ============== Dynamic Resource Injection ==============

    def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
        """动态注入相关资源 — 向量检索 + 脑区过滤检索。

        两条检索路径并行:
        1. 全局向量检索 (search_multi_lightrag) — 语义最相关的 top_k 实体
        2. 脑区内过滤检索 (search_within_region) — 激活脑区成员中语义最匹配的实体

        Args:
            context: 3条对话上下文
        """
        # 0. Brain region activation
        effective_query = context
        keywords = [effective_query]
        _brain_injector = None
        try:
            _brain_injector = self._get_brain_injector()
            if _brain_injector is not None:
                _brain_injector.activate_for_query(context)
        except Exception as e:
            logger.warning(f"Brain activation failed: {e}")

        # 1. LightRAG 全局检索 — local + keywords = 0 LLM calls
        lightrag_results: dict[str, list[dict]] = {}
        adapter = None
        try:
            if self._brain_adapter is not None:
                adapter = self._brain_adapter
            else:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
            lightrag_results = adapter.search_multi_lightrag(
                effective_query, mode="local", top_k=10, keywords=keywords,
            )
        except Exception as e:
            logger.warning(f"LightRAG retrieval failed: {e}")

        # 2. 脑区内过滤检索 — 激活脑区成员范围内语义搜索
        region_results: dict[str, list[dict]] = {"skill": [], "knowledge": [], "other": []}
        try:
            if _brain_injector is not None:
                active_regions = _brain_injector.get_active_regions()
                if active_regions:
                    all_region_members = set()
                    for region in active_regions:
                        members = _brain_injector.get_members_of_region(region.region_id)
                        all_region_members.update(members)
                    if all_region_members:
                        region_adapter = adapter
                        if region_adapter is None:
                            from niu_api.internal.lightrag_adapter import LightRAGAdapter
                            region_adapter = LightRAGAdapter()
                        region_results = region_adapter.search_within_region(
                            effective_query,
                            region_member_names=all_region_members,
                            mode="local",
                            top_k=10,
                            keywords=keywords,
                        )
        except Exception as e:
            logger.warning(f"Region-filtered search failed: {e}")

        # 3. interaction_habits（LightRAG + keywords）
        interaction_habits: list[dict] = []
        try:
            if self._brain_adapter is not None:
                habit_adapter = self._brain_adapter
            else:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                habit_adapter = LightRAGAdapter()
            interaction_habits = habit_adapter.search_interaction_habits(
                query=effective_query, top_k=3, keywords=keywords,
            )
        except Exception as e:
            logger.debug(f"Interaction habits search failed (non-blocking): {e}")

        # 4. Brain graph memory recall
        brain_memories_text = ""
        try:
            from niu_api.internal.brain_graph import get_brain_graph, format_memories_for_prompt
            bg = get_brain_graph()
            brain_memories = bg.recall_memories(context, top_k=10, min_weight=0.3, keywords=keywords)
            if brain_memories:
                brain_memories_text = format_memories_for_prompt(brain_memories)
        except Exception as e:
            logger.debug(f"Brain graph recall failed (non-blocking): {e}")

        # ============== Format & Inject ==============
        parts = []
        seen_names: set[str] = set()

        logger.debug(
            f"Dynamic injection | "
            f"Skills: {len(lightrag_results.get('skill', []))}, "
            f"Knowledge: {len(lightrag_results.get('knowledge', []))}, "
            f"Region skills: {len(region_results.get('skill', []))}, "
            f"Region knowledge: {len(region_results.get('knowledge', []))}, "
            f"Habits: {len(interaction_habits)}"
        )

        # Brain region status map (always inject)
        try:
            if _brain_injector is not None:
                brain_context = _brain_injector.format_region_map_only()
                if brain_context:
                    parts.append(f"\n{brain_context}")
        except Exception as e:
            logger.warning(f"Brain region map injection failed: {e}")

        # Skills (global vector search)
        lightrag_skills = lightrag_results.get("skill", [])
        skills_text, seen_names = self._format_lightrag_entities_for_prompt(
            lightrag_skills, "相关技能", seen_names,
        )
        if skills_text:
            parts.append(skills_text)

        # Knowledge (global vector search)
        lightrag_knowledge = lightrag_results.get("knowledge", [])
        knowledge_text, seen_names = self._format_lightrag_entities_for_prompt(
            lightrag_knowledge, "参考知识", seen_names,
        )
        if knowledge_text:
            parts.append(knowledge_text)

        # Region-filtered knowledge (brain region semantic search, deduped with seen_names)
        region_knowledge = region_results.get("knowledge", [])
        region_skills = region_results.get("skill", [])
        region_all = region_skills + region_knowledge
        if region_all:
            region_text, seen_names = self._format_lightrag_entities_for_prompt(
                region_all, "活跃脑区知识", seen_names,
            )
            if region_text:
                parts.append(region_text)
                parts.append(
                    "\n\n### [知识探索指引]\n"
                    "优先参考上述活跃脑区知识回答用户问题，脑区内容与你当前关注领域最相关。"
                )

        # Interaction habits (LightRAG)
        if interaction_habits:
            habits_text, seen_names = self._format_lightrag_entities_for_prompt(
                interaction_habits, "交互习惯", seen_names,
            )
            if habits_text:
                parts.append(habits_text)

        # Brain memories
        brain_memories_text = _strip_lightrag_error_lines(brain_memories_text)
        if brain_memories_text:
            parts.append(brain_memories_text)

        injection = "\n".join(parts)
        if injection:
            logger.debug(f"Dynamic injection - Total length: {len(injection)} chars")
        else:
            logger.debug("Dynamic injection - Skipped (no relevant results)")

        return injection, {}

    def chat(
        self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None, resources: list | None = None
    ) -> Generator[str, None, None]:
        """执行对话 — disk mode: base tools + disk only.

        Args:
            session_id: 会话ID
            user_input: 用户输入
            stream: 是否流式输出
            max_turns: 最大轮次
            history: 可选的历史消息列表
        """
        # 从消息历史中提取上下文
        context = self._extract_context_from_history(history, user_input)

        # 动态注入资源（skills/knowledge only）
        injection, _ = self._inject_dynamic_resources(context)

        # 组装 system_prompt
        system_prompt = self.base_system_prompt
        if injection:
            system_prompt += injection

        # 注入 resources（拖入文件的模式信息）
        if resources:
            # 防御性过滤：只处理格式正确的资源条目
            valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
            if valid_resources:
                resource_lines = []
                for r in valid_resources:
                    path = r.get("path", "")
                    mode = r.get("mode", "copy")
                    if mode == "reference":
                        resource_lines.append(f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用")
                    elif mode == "move":
                        resource_lines.append(f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录")
                    # mode="copy" 不需要额外提示，这是默认行为
                if resource_lines:
                    system_prompt += "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n" + "\n".join(resource_lines)

        # 组装 tools_schema = base tools + static MCP tools + disk
        tools_schema = self.base_tools_schema.copy()

        # Inject static-visibility MCP tools (e.g. brain_region/*)
        # These are always visible to the LLM, unlike hidden/dynamic tools
        # which are accessed via disk().
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            for tool_name in registry.get_static_tools():
                schema = registry._schemas.get(tool_name)
                if schema:
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": schema["name"],
                            "description": schema.get("description", ""),
                            "parameters": schema.get("input_schema", {"type": "object", "properties": {}}),
                        }
                    })
        except Exception as e:
            logger.debug(f"Static MCP tools injection skipped: {e}")

        # Add disk tool
        disk_schema = self.disk_engine.get_schema()
        tools_schema.append(disk_schema)

        logger.debug(
            f"tools_schema: {len(self.base_tools_schema)} base + {len(tools_schema) - len(self.base_tools_schema) - 1} static + 1 disk = {len(tools_schema)} total"
        )

        # 读取上下文窗口大小，用于主 Agent 85% 溢出检测
        from agent.subagent import _read_context_window_tokens
        context_window_tokens = _read_context_window_tokens()

        gen = agent_runner_loop(
            client=self.client,
            system_prompt=system_prompt,
            user_input=user_input,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=max_turns,
            verbose=False,
            initial_user_content=user_input,
            history=history,  # Pass history to agent_loop
            on_turn_end=self._on_turn_end,  # 每轮结束后刷新动态注入
            context_window_tokens=context_window_tokens,  # 主 Agent 溢出检测
        )

        # 累加输出（双管道：full_resp 只含 reply 内容，用于 DB 存储）
        full_resp = ""
        return_value = None
        self.last_return_value = None  # 重置，避免复用残留
        persisted_msgs = []  # V4: 已通过persist事件持久化的消息列表
        chat_idle_pushed = False  # 跟踪是否已推送 chat_idle，避免重复推送
        try:
            while True:
                # 协作式停止：每次迭代检查，发现停止立即停止消费生成器
                if is_stop_requested():
                    logger.info("[Runner] Stop requested, stopping generator consumption")
                    gen.close()  # 关闭生成器，触发 GeneratorExit
                    break
                try:
                    chunk = next(gen)
                    if isinstance(chunk, StreamEvent):
                        if chunk.type == "reply":
                            full_resp += chunk.content
                            if chunk.content:  # SSE 管道：只推送非空 reply
                                yield chunk.content
                        elif chunk.type == "persist":
                            # V4: 逐条持久化消息到 DB + 通知 SSE
                            try:
                                msg_dict = json.loads(chunk.content)
                                msg_id = self._persist_one_msg(msg_dict)
                                if msg_id is not None:
                                    msg_dict["_persisted_id"] = msg_id  # 记录写入后的消息ID
                                    persisted_msgs.append(msg_dict)
                            except Exception as e:
                                logger.warning(f"[Runner] Failed to persist msg: {e}")
                        elif chunk.type == "system":
                            # V4: chat_busy/chat_idle 状态机事件，通过SSE推送给前端
                            if chunk.content in ("chat_busy", "chat_idle"):
                                from niu_api.chat import notify_new_message_sync
                                notify_new_message_sync("", chunk.content, "", source="electron")
                                if chunk.content == "chat_idle":
                                    chat_idle_pushed = True
                        # type="tool_marker" 不进入 SSE 和 full_resp
                    else:
                        # 向后兼容：普通 str
                        full_resp += chunk
                        if chunk:
                            yield chunk
                except StopIteration as e:
                    return_value = e.value
                    break
        finally:
            # 确保停止标志被清除（无论正常退出、停止退出还是异常退出）
            if is_stop_requested():
                clear_stop()
            # 防御性推送 chat_idle：gen.close() 可能中断 agent_loop 的正常退出路径
            # 只在未推送过时才推送，避免重复
            if not chat_idle_pushed:
                from niu_api.chat import notify_new_message_sync
                notify_new_message_sync("", "chat_idle", "", source="electron")

        # 暴露 return_value 给调用方（用于检测 CONTEXT_OVERFLOW 等控制流）
        self.last_return_value = return_value
        self._persisted_msgs = persisted_msgs  # V4: 已逐条持久化的消息列表

        # 如果 full_resp 为空但有返回值数据，使用返回值
        if not full_resp.strip() and return_value:
            if isinstance(return_value, dict) and "data" in return_value:
                data = return_value["data"]

                try:
                    # 处理有 content 属性的对象（如 MockResponse）
                    if hasattr(data, 'content'):
                        parts = []

                        # 提取思考链（如果有）
                        if hasattr(data, 'thinking') and data.thinking:
                            parts.append(f"<thinking>\n{data.thinking}\n</thinking>")

                        # 提取内容（确保不为 None）
                        if data.content is not None:
                            parts.append(str(data.content))

                        full_resp = "\n\n".join(parts) if parts else ""

                    # 处理字典
                    elif isinstance(data, dict):
                        full_resp = json.dumps(data, ensure_ascii=False)

                    # 处理列表
                    elif isinstance(data, list):
                        full_resp = json.dumps(data, ensure_ascii=False)

                    # 处理字符串
                    elif isinstance(data, str):
                        full_resp = data

                    # 处理其他类型
                    elif data is not None:
                        full_resp = str(data)
                    else:
                        full_resp = ""

                except Exception as e:
                    # 异常保护：记录错误但不崩溃
                    logger.error(f"Failed to extract return_value data: {e}")
                    full_resp = ""

            # 回退：return_value 中提取的内容作为最后一个 chunk yield
            if full_resp.strip():
                yield full_resp.strip()

        # 对话结束后工具衰减已由 _on_turn_end 每轮执行，此处不再重复

    def _persist_one_msg(self, msg_dict: dict) -> str | None:
        """逐条持久化消息到 DB + 通知 SSE（同步，从 executor 线程调用）

        Args:
            msg_dict: 完整的消息 dict，包含 role, content, tool_calls, tool_call_id 等

        Returns:
            消息 ID，或 None（写入失败或消息被过滤）
        """
        from niu_api.chat import notify_new_message_sync
        from agent.session import get_message_store

        role = msg_dict.get("role", "")
        content = msg_dict.get("content", "") or ""
        tool_calls = msg_dict.get("tool_calls")
        tool_call_id = msg_dict.get("tool_call_id", "")

        # 同步写入 DB
        msg_id = self._sync_add_message(role=role, content=content,
                                         tool_calls=tool_calls, tool_call_id=tool_call_id)
        if msg_id is None:
            return None

        # 通知 SSE（仅 assistant 消息推送给前端）
        if role == "assistant" and content.strip():
            notify_new_message_sync(msg_id, "assistant", content, source="electron")

            # 触发飞书流式推送
            try:
                from niu_api.channel.feishu_channel import FeishuChannelAdapter
                FeishuChannelAdapter.trigger_feishu_stream_push()
            except Exception:
                pass

        return msg_id

    def _sync_add_message(self, role: str, content: str,
                           tool_calls: list | None = None, tool_call_id: str = "") -> str | None:
        """从同步线程写入消息到 DB（桥接 aiosqlite）

        使用 asyncio.run_coroutine_threadsafe 在 FastAPI 事件循环中执行 DB 写入，
        然后阻塞等待结果。这保证了消息按 yield 顺序写入 DB（不会倒序）。

        超时设为30秒：DB写入正常情况下<100ms，30秒足够覆盖极端情况。
        如果30秒仍超时，说明DB严重故障，此时重复写入是可接受的。

        Returns:
            消息 ID，或 None（写入失败）
        """
        from niu_api.chat import _main_loop
        from agent.session import get_message_store
        import asyncio

        loop = _main_loop
        if loop is None or loop.is_closed():
            logger.warning("[Runner] No event loop available for sync DB write")
            return None

        async def _do_add():
            store = await get_message_store()
            return await store.add_message(
                role=role, content=content,
                tool_calls=tool_calls, tool_call_id=tool_call_id
            )

        try:
            future = asyncio.run_coroutine_threadsafe(_do_add(), loop)
            msg_id = future.result(timeout=30.0)  # 阻塞等待，保证顺序
            return msg_id
        except Exception as e:
            logger.warning(f"[Runner] sync_add_message failed: {e}")
            return None


# 全局实例
_runner: Optional[NiuRunner] = None
_runner_lock = threading.Lock()


def get_runner(llm_config: Optional[Dict[str, Any]] = None, mcp_client=None) -> Optional[NiuRunner]:
    """获取全局 Runner 实例（线程安全）"""
    global _runner
    if _runner is None and llm_config:
        with _runner_lock:
            # 双重检查
            if _runner is None:
                _runner = NiuRunner(llm_config, mcp_client)
    return _runner


def chat(session_id: str, user_input: str, **kwargs) -> Generator[str, None, None]:
    """便捷函数：执行对话"""
    runner = get_runner()
    if runner is None:
        raise RuntimeError("Runner not initialized. Call get_runner() with config first.")
    return runner.chat(session_id, user_input, **kwargs)

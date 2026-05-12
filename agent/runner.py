"""
Niu Agent Runner

简化的 Agent 入口，直接使用 GenericAgent 组件。
Disk mode: MCP 工具通过虚拟磁盘 disk() 发现和调用，
Skills/知识通过 LightRAG 动态注入提示词。
"""

import json
import os
import re
import sys
import io
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from loguru import logger

from niu_api.internal.lightrag_adapter import _LIGHTRAG_ERROR_MARKERS


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

from .generic.agent_loop import agent_runner_loop
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

    # 工作环境
    workspace = memory.get("workspace", {})
    if workspace.get("path"):
        ws_str = f"## 工作环境\n\n知识库目录：{workspace['path']}"
        parts.append(ws_str)

    # 用户信息
    user = memory.get("user", {})
    if user.get("name"):
        user_str = f"## 用户信息\n\n用户称呼：{user['name']}"
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
        try:
            agent_config = get_subagent_config(agent_name)
            desc = agent_config.get("description", f"子 Agent: {agent_name}")
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
                                "description": "任务描述，如：处理照片：E:/path/photo.jpg",
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

    def _format_lightrag_entities_for_prompt(
        self, entities: list[dict], title: str, seen_names: set[str],
    ) -> tuple[str, set[str]]:
        """Format LightRAG entity dicts for prompt injection.

        Similar to format_resources_for_prompt but works with LightRAG
        entity dicts (entity_name, description) instead of SearchResult objects.

        Args:
            entities: List of LightRAG entity dicts.
            title: Section title for the prompt.
            seen_names: Set of names already included (for dedup).

        Returns:
            Tuple of (formatted_text, updated_seen_names).
        """
        if not entities:
            return "", seen_names

        # Detect if this is a skill section (for path annotation)
        is_skill_section = title == "相关技能"

        lines = [f"\n\n### [{title}]"]
        added = 0
        for entity in entities:
            entity_name = entity.get("entity_name", "")
            # Entity names use natural language (no type prefixes)
            display_name = entity_name
            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            description = entity.get("description", "")
            if description:
                lines.append(f"{added + 1}. **{display_name}** (来源: 知识图谱)")
                lines.append(f"   {description}")
            else:
                lines.append(f"{added + 1}. **{display_name}** (来源: 知识图谱)")
            # For skill entities, append file path so LLM can read the full file
            if is_skill_section:
                lines.append(f"   路径: memory/skills/{display_name}.md")
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
            "你有一个虚拟磁盘工具 disk(command)，可以用 Unix 命令探索和调用所有 MCP 工具。\n\n"
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
        """动态注入相关资源（Skills、知识）— no MCP tool scores.

        LightRAG 图检索为主检索路径，使用 local + keywords 模式
        实现 0 LLM 调用、完整图遍历的快速检索。
        interaction_habits 也从 LightRAG 读取。

        Args:
            context: 3条对话上下文（包含历史消息和当前用户输入）

        检索顺序：
        1. LightRAG 主检索（local + keywords 模式）→ skills + knowledge
        2. interaction_habits（LightRAG + keywords）
        3. brain memories（脑图）
        """
        # 1. LightRAG 主检索 — local + keywords = 0 LLM calls
        effective_query = context
        keywords = [effective_query]
        lightrag_results: dict[str, list[dict]] = {}
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            if self._brain_adapter is not None:
                adapter = self._brain_adapter
            else:
                adapter = LightRAGAdapter()
            lightrag_results = adapter.search_multi_lightrag(
                effective_query, mode="local", top_k=10, keywords=keywords,
            )
        except Exception as e:
            logger.warning(f"LightRAG retrieval failed: {e}")

        if lightrag_results and not any(lightrag_results.values()):
            logger.debug("LightRAG returned empty results")

        # 2. interaction_habits（LightRAG + keywords）
        interaction_habits: list[dict] = []
        try:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            if self._brain_adapter is not None:
                habit_adapter = self._brain_adapter
            else:
                habit_adapter = LightRAGAdapter()
            interaction_habits = habit_adapter.search_interaction_habits(
                query=effective_query, top_k=3, keywords=keywords,
            )
        except Exception as e:
            logger.debug(f"Interaction habits search failed (non-blocking): {e}")

        # 3. Brain graph memory recall
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
            f"Habits: {len(interaction_habits)}"
        )

        # Brain region activation context (uses cached injector)
        # Apply activation weight BEFORE formatting so weighted scores affect output
        lightrag_skills = lightrag_results.get("skill", [])
        lightrag_knowledge = lightrag_results.get("knowledge", [])
        try:
            _brain_injector = self._get_brain_injector()
            if _brain_injector is not None:
                brain_context = _brain_injector.inject_brain_context(context)
                if brain_context:
                    parts.append(f"\n## 脑区激活上下文\n{brain_context}")
                    logger.debug(f"Brain context injected: {len(brain_context)} chars")

                # Apply activation weight to LightRAG search results
                # lightrag_skills and lightrag_knowledge are list[dict], each has "score" field
                # Weighting boosts scores for entities in activated regions
                if lightrag_skills:
                    lightrag_skills[:] = _brain_injector.apply_activation_weight(lightrag_skills)
                if lightrag_knowledge:
                    lightrag_knowledge[:] = _brain_injector.apply_activation_weight(lightrag_knowledge)
        except Exception as e:
            logger.debug(f"BrainContextInjector not available: {e}")

        # Skills (after weighting)
        skills_text, seen_names = self._format_lightrag_entities_for_prompt(
            lightrag_skills, "相关技能", seen_names,
        )
        if skills_text:
            parts.append(skills_text)
            parts.append(
                "\n\n### [技能使用指引]\n"
                "上述技能仅展示了名称和触发描述。当你判断某个技能与当前任务相关时，"
                "必须使用 file_read 读取完整技能文件后再执行，路径格式：memory/skills/<技能名>.md\n"
                "不要仅凭描述猜测技能用法。"
            )

        # Knowledge (after weighting)
        knowledge_text, seen_names = self._format_lightrag_entities_for_prompt(
            lightrag_knowledge, "参考知识", seen_names,
        )
        if knowledge_text:
            parts.append(knowledge_text)
            parts.append(
                "\n\n### [知识探索指引]\n"
                "优先参考上述注入的历史参考信息回答用户问题。"
                "若命中知识点涉及已知实体，可使用 disk(\"/lightrag/query <实体名>\") 查询知识图谱。"
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

        return injection, {}  # Empty mcp_tool_scores — no dynamic MCP injection

    def chat(
        self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None
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

        # 组装 tools_schema = base tools + disk
        tools_schema = self.base_tools_schema.copy()

        # Add disk tool
        disk_schema = self.disk_engine.get_schema()
        tools_schema.append(disk_schema)

        logger.debug(
            f"tools_schema: {len(self.base_tools_schema)} base + 1 disk = {len(tools_schema)} total"
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

        # 累加输出
        full_resp = ""
        return_value = None
        self.last_return_value = None  # 重置，避免复用残留
        while True:
            try:
                chunk = next(gen)
                full_resp += chunk
            except StopIteration as e:
                return_value = e.value
                break

        # 暴露 return_value 给调用方（用于检测 CONTEXT_OVERFLOW 等控制流）
        self.last_return_value = return_value

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

        # 清理 CLI 调试输出
        full_resp = re.sub(r"\*\*LLM Running \(Turn \d+\) \.\.\.\*\*\n*", "", full_resp)
        full_resp = re.sub(r"🛠️ \*\*正在调用工具:[\s\S]*?````\n", "", full_resp)
        full_resp = re.sub(r"<summary>[\s\S]*?</summary>\n*", "", full_resp, flags=re.IGNORECASE)

        # 清理内部工具调用标签（LiteLLM 调试输出，不应显示给用户）
        full_resp = re.sub(r"<tool_use>.*?</tool_use>", "", full_resp, flags=re.DOTALL)

        # 清理 LLM 输出的结构化标签（如 <text>, </text>）
        full_resp = re.sub(r"</?text>\n*", "", full_resp)
        full_resp = re.sub(r"</?response>\n*", "", full_resp)
        full_resp = re.sub(r"</?content>\n*", "", full_resp)

        # 清理空代码块（可能来自 LLM 响应）
        # 先处理多个连续反引号的情况
        full_resp = re.sub(r"`{6,}\n*", "", full_resp)  # 6个及以上反引号
        full_resp = re.sub(r"`{5}\n*", "", full_resp)  # 5个反引号
        full_resp = re.sub(r"`{4}\n*", "", full_resp)  # 4个反引号
        full_resp = re.sub(r"```\s*```\n*", "", full_resp)  # 连续的空代码块

        # 清理开头和结尾的单独反引号（无内容的代码块标记）
        # 开头的 ``` 后面没有内容或直接换行
        full_resp = re.sub(r"^```\s*\n", "", full_resp)
        full_resp = re.sub(r"^```\s*$", "", full_resp, flags=re.MULTILINE)
        # 结尾的 ```
        full_resp = re.sub(r"\n```\s*$", "", full_resp)
        full_resp = re.sub(r"```\s*$", "", full_resp)

        # 清理中间的孤立反引号行（整行只有反引号）
        full_resp = re.sub(r"\n```\s*\n", "\n", full_resp)
        full_resp = re.sub(r"\n```\s*", "\n", full_resp)

        # 清理连续空行（超过2个空行变成2个）
        full_resp = re.sub(r"\n{3,}", "\n\n", full_resp)

        # 对话结束后工具衰减已由 _on_turn_end 每轮执行，此处不再重复

        yield full_resp.strip()


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

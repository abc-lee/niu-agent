"""
Niu Agent Runner

简化的 Agent 入口，直接使用 GenericAgent 组件。
集成动态注入：Skills 按语义注入提示词，MCP 工具按分数动态注入 tools_schema。
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
from .vector_search import get_vector_search
from .injector.sync import get_skill_sync
from .tool_lifecycle import ToolLifecycleManager
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
            "2. 写入 ~/.niu/memory.json：设置 workspace.path，删除 firstRun 字段\n\n"
            "完成后，下次对话不再出现此提示。"
        )

    return "\n\n".join(parts)


def get_tools_schema() -> list:
    """获取工具 Schema（排除不支持的工具 + 注册子 Agent 工具）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(script_dir, "generic", "assets", "tools_schema.json")

    excluded_tools = {"ask_user"}  # 前端不支持的工具

    tools = []
    if os.path.exists(schema_path):
        with open(schema_path, "r", encoding="utf-8") as f:
            all_tools = json.load(f)
        tools = [t for t in all_tools if t.get("function", {}).get("name") not in excluded_tools]

    # 注册子 Agent 工具
    sub_agent_descriptions = {
        "file-processor": "【必须调用】处理文件和照片：入库、人脸识别、文档解析。用户拖入文件/照片时必须调用此工具，不要自己处理文件。",
        "event-manager": "处理日程、提醒、定时任务。",
        "context-manager": "记忆压缩、上下文整理。",
    }
    for agent_name, desc in sub_agent_descriptions.items():
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
        self.handler = NiuHandler(mcp_client=mcp_client)
        self.base_system_prompt = get_system_prompt()
        self.base_tools_schema = get_tools_schema()
        self.vector_search = get_vector_search()

        # 启动 Skills 后台同步
        get_skill_sync(auto_start=True)

        # MCP 工具列表（启动时加载，缓存）
        self._mcp_tools_schema: list = []

        # 工具生命周期管理
        self.tool_lifecycle = ToolLifecycleManager(decay_rate=10, remove_threshold=25)

        # 用户记忆脏标记（remember/forget 工具调用后 set）
        self._memory_dirty = threading.Event()

    def _get_static_tools(self) -> list:
        """获取 visibility=static 的工具名列表（替代硬编码 BASE_MCP_TOOLS）"""
        return get_registry().get_static_tools()

    def set_mcp_tools_schema(self, tools: list):
        """设置 MCP 工具 Schema（从外部调用）

        过滤掉 visibility=hidden 的工具，不注入到 _mcp_tools_schema
        """
        registry = get_registry()
        schema = []
        for tool in tools:
            tool_name = tool["name"]
            # 跳过 visibility=hidden 的工具
            if registry.get_visibility(tool_name) == "hidden":
                continue
            schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool.get("description", ""),
                        "parameters": tool.get(
                            "input_schema", {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        self._mcp_tools_schema = schema
        logger.info(f"Loaded {len(schema)} MCP tools")

    def _get_tool_schema_by_name(self, tool_name: str) -> Optional[Dict]:
        """
        根据工具名获取工具Schema

        Args:
            tool_name: 工具名，格式为 "server-name/tool-name"

        Returns:
            工具Schema字典，找不到返回None
        """
        for tool in self._mcp_tools_schema:
            if tool.get("function", {}).get("name") == tool_name:
                return tool
        return None

    def _build_dynamic_tools_schema(self, static_tools: set) -> list:
        """组装动态工具 schema（top-10 按分数降序，同分并列）

        static_tools 不参与排序，单独注入不受限。
        只对动态工具排序截断，避免 static 占 top-10 名额。
        """
        MAX_DYNAMIC_TOOLS = 10
        active_tool_names = self.tool_lifecycle.get_active_tools()

        # 排除 static 工具，只对动态工具排序截断
        scored = [(name, self.tool_lifecycle.get_tool_score(name))
                  for name in active_tool_names if name not in static_tools]
        scored.sort(key=lambda x: -x[1])

        # top-10，同分数并列不截断
        top = scored[:MAX_DYNAMIC_TOOLS]
        if len(scored) > MAX_DYNAMIC_TOOLS:
            cutoff_score = top[-1][1]
            for name, score in scored[MAX_DYNAMIC_TOOLS:]:
                if score == cutoff_score:
                    top.append((name, score))
                else:
                    break

        result = []
        for tool_name, _ in top:
            schema = self._get_tool_schema_by_name(tool_name)
            if schema:
                result.append(schema)
        return result

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
        """
        每轮循环结束后刷新动态注入。

        Args:
            messages: 当前消息列表（可修改 messages[0] 更新 system_prompt）
            tools_schema: 当前工具 Schema 列表（可修改/返回新列表）
            turn: 当前轮次

        Returns:
            更新后的 tools_schema
        """
        # 1. 先衰减所有工具分数（每轮 -10）
        self.tool_lifecycle.decay_tools()

        # 0. Refresh user memories if dirty
        self._refresh_user_memories(messages)

        # 2. 从 messages 中提取最新上下文
        context = self._extract_context_from_messages(messages)

        # 3. 重新执行动态注入
        injection, mcp_tool_scores = self._inject_dynamic_resources(context)

        # 4. 向量检索到的工具分数：和衰减后分数取大值
        for tool_name, search_score in mcp_tool_scores.items():
            self.tool_lifecycle.update_from_search(tool_name, search_score)

        # 5. 更新 system_prompt（messages[0]）— always update so dirty refresh takes effect
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = self.base_system_prompt + injection

        # 6. 重新组装 tools_schema（加入新发现的工具）
        new_schema = self.base_tools_schema.copy()
        static_tools = set(self._get_static_tools())
        for tool_name in static_tools:
            schema = self._get_tool_schema_by_name(tool_name)
            if schema:
                new_schema.append(schema)

        # 动态注入其他工具（top-10 按分数降序，同分并列）
        dynamic_schemas = self._build_dynamic_tools_schema(static_tools)
        new_schema.extend(dynamic_schemas)

        return new_schema

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

        # Also update the live message if it exists
        if messages and messages[0].get("role") == "system":
            content = messages[0]["content"]
            if re.search(pattern, content, re.DOTALL):
                if new_section:
                    messages[0]["content"] = re.sub(pattern, new_section, content, flags=re.DOTALL)
                else:
                    messages[0]["content"] = re.sub(r'\n*' + pattern + r'\n*', '', content, flags=re.DOTALL)
            elif new_section:
                messages[0]["content"] = content + "\n\n" + new_section

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

    def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
        """
        动态注入相关资源（Skills、知识）+ MCP 工具分数提取

        从向量库搜索相关资源，返回格式化的提示词扩展和 MCP 工具分数

        Args:
            context: 3条对话上下文（包含历史消息和当前用户输入）

        注入顺序：
        1. 活跃Skills（工具命中后激活）
        2. 语义匹配的Skills
        3. MCP工具 → 仅提取分数供 tool_lifecycle.update_from_search，不注入提示词
        4. 知识文档
        5. 交互习惯

        阈值策略：
        - Skills: 0.25
        - MCP工具: 0.25
        - 知识: 0.3
        - 交互习惯: 0.4
        """
        # 1. 一次向量检索，按 category 分组返回（避免同一 context 多次 embedding 计算）
        multi_results = self.vector_search.search_multi(
            query=context,
            categories={
                "skill": {"limit": 3, "min_score": 0.25},
                "mcp_tool": {"limit": 10, "min_score": 0.25},
                "document": {"limit": 20, "min_score": 0.3},
                "interaction_habit": {"limit": 3, "min_score": 0.4},
            },
            enable_recursion=True
        )
        skills = multi_results.get("skill", [])
        mcp_tools = multi_results.get("mcp_tool", [])
        knowledge = multi_results.get("document", [])
        interaction_habits = multi_results.get("interaction_habit", [])

        # 2. 用本轮工具名做 skill 精确检索（替代原 _activate_related_skills 的即时检索）
        tool_signal_skills = []
        recent_tool_names = self.tool_lifecycle.consume_recent_hits()
        seen_tools = set()
        for tool_name in recent_tool_names:
            if tool_name in seen_tools:
                continue
            seen_tools.add(tool_name)
            try:
                tool_skills = self.vector_search.search(
                    query=tool_name,
                    limit=2,
                    min_score=0.3,
                    metadata_filter={"category": "skill"}
                )
                tool_signal_skills.extend(tool_skills)
            except Exception:
                pass

        if tool_signal_skills:
            logger.debug(f"Tool-signal Skills: {len(tool_signal_skills)} results")

        logger.debug(f"Dynamic injection - Skills: {len(skills)}, MCP: {len(mcp_tools)}, Knowledge: {len(knowledge)}, Habits: {len(interaction_habits)}, ToolSignalSkills: {len(tool_signal_skills)}")

        # 3.5 向量检索到的 MCP 工具：返回分数供 update_from_search（不再注入 system prompt，tools_schema 已有完整描述）
        # 过滤 hidden（不可见）和 static（已固定注入，不需要动态分数）的工具
        mcp_tool_scores = {}
        registry = get_registry()
        for tool in mcp_tools:
            name = tool.metadata.get("name", "")
            server = tool.metadata.get("server", "")
            full_name = f"{server}/{name}" if server else name
            score = tool.score if hasattr(tool, "score") else 0
            if not full_name or score <= 0:
                continue
            vis = registry.get_visibility(full_name)
            if vis == "hidden" or vis == "static":
                continue
            mcp_tool_scores[full_name] = int(score * 100)

        # 格式化
        parts = []
        # 合并工具名检索Skills和搜索到的Skills（去重）
        all_skills = tool_signal_skills + skills
        if all_skills:
            # 去重：按metadata.name去重，name为空时用id兜底
            seen_names = set()
            unique_skills = []
            for skill in all_skills:
                name = skill.metadata.get("name", "") or skill.id
                if name and name not in seen_names:
                    seen_names.add(name)
                    unique_skills.append(skill)

            if unique_skills:
                parts.append(format_resources_for_prompt(unique_skills, "相关技能"))

        if knowledge:
            parts.append(format_resources_for_prompt(knowledge, "参考知识"))
            parts.append(
                "\n\n### [知识探索指引]\n"
                "优先参考上述注入的历史参考信息回答用户问题。"
                "若命中知识点涉及已知实体（人名、技术、组织等），"
                "可使用 `kg-server/explore_node` 或 `kg-server/get_related_entities` "
                "查询知识图谱中的关联信息，获取更完整的上下文。"
            )
        if interaction_habits:
            parts.append(format_resources_for_prompt(interaction_habits, "交互习惯"))

        injection = "\n".join(parts)
        if injection:
            logger.debug(f"Dynamic injection - Total length: {len(injection)} chars")
        else:
            logger.debug("Dynamic injection - Skipped (no relevant results)")

        return injection, mcp_tool_scores

    def chat(
        self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None
    ) -> Generator[str, None, None]:
        """
        执行对话

        动态注入：
        1. 根据用户输入搜索 Skills、MCP 工具描述、知识
        2. 组装 system_prompt = base_prompt + 动态资源
        3. 组装 tools_schema = base_tools + mcp_tools

        Args:
            session_id: 会话ID
            user_input: 用户输入
            stream: 是否流式输出
            max_turns: 最大轮次
            history: 可选的历史消息列表 [{"role": "user/assistant", "content": str}, ...]
        """
        # 重置会话级状态
        self.tool_lifecycle.reset_session()

        # 从消息历史中提取上下文（用于向量检索）
        context = self._extract_context_from_history(history, user_input)

        # 动态注入资源
        injection, mcp_tool_scores = self._inject_dynamic_resources(context)

        # 向量检索到的工具分数：和当前分数取大值
        for tool_name, search_score in mcp_tool_scores.items():
            self.tool_lifecycle.update_from_search(tool_name, search_score)

        # 组装 system_prompt
        system_prompt = self.base_system_prompt
        if injection:
            system_prompt += injection

        # 组装 tools_schema = 内置工具 + 静态MCP工具 + 动态MCP工具(top-10)
        tools_schema = self.base_tools_schema.copy()

        # 固定注入静态工具（visibility=static）
        static_tools = set(self._get_static_tools())
        for tool_name in static_tools:
            schema = self._get_tool_schema_by_name(tool_name)
            if schema:
                tools_schema.append(schema)

        # 动态注入其他工具（top-10 按分数降序，同分并列）
        dynamic_schemas = self._build_dynamic_tools_schema(static_tools)
        tools_schema.extend(dynamic_schemas)

        # 调试：打印工具数量
        logger.debug(
            f"tools_schema: {len(self.base_tools_schema)} base + {len(static_tools)} static + {len(dynamic_schemas)} dynamic = {len(tools_schema)} total"
        )

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
        )

        # 累加输出
        full_resp = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                full_resp += chunk
            except StopIteration as e:
                return_value = e.value
                break

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

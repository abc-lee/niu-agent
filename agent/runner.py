"""
Niu Agent Runner

简化的 Agent 入口，直接使用 GenericAgent 组件。
集成动态注入：Skills 和 MCP 工具描述按语义注入提示词。
"""

import json
import os
import re
import sys
import io
import threading
from datetime import datetime
from typing import Any, Dict, Generator, Optional

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

# 主Agent基础MCP工具列表（固定注入）
BASE_MCP_TOOLS = [
    # memory-server (6个)
    "memory-server/remember",
    "memory-server/recall",
    "memory-server/update_memory",
    "memory-server/get_memory_stats",
    "memory-server/cleanup_memories",
    "memory-server/link_memories",

    # vector-store (5个)
    "vector-store/add_document",
    "vector-store/search_documents",
    "vector-store/get_document",
    "vector-store/delete_document",
    "vector-store/list_documents",

    # browser-server (1个)
    "browser-server/browser_navigate",
]


def get_system_prompt() -> str:
    """获取系统提示词"""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. 读取核心提示词
    sys_prompt_path = os.path.join(script_dir, "generic", "assets", "sys_prompt.txt")
    if os.path.exists(sys_prompt_path):
        with open(sys_prompt_path, "r", encoding="utf-8") as f:
            sys_prompt = f.read()
    else:
        sys_prompt = "# Role: Niu Agent\nYou are a helpful assistant with file and code access."

    # 2. 追加 niu.md 配置
    niu_md_path = os.path.join(script_dir, "..", "config", "agents", "niu.md")
    if os.path.exists(niu_md_path):
        with open(niu_md_path, "r", encoding="utf-8") as f:
            content = f.read()
            if "---" in content:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    sys_prompt += "\n\n" + parts[2].strip()

    # 3. 注入 memory.json 中的身份设定和用户偏好
    memory_section = _load_memory_for_prompt()
    if memory_section:
        sys_prompt += "\n\n" + memory_section

    # 4. 添加当前时间
    now = datetime.now()
    sys_prompt += f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

    return sys_prompt


def _load_memory_for_prompt() -> str:
    """从 memory.json 加载身份设定和用户偏好，格式化为提示词"""
    import json
    from pathlib import Path

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
    if user.get("name") or user.get("preferences"):
        user_str = "## 用户信息\n\n"
        if user.get("name"):
            user_str += f"用户称呼：{user['name']}\n"
        if user.get("preferences"):
            user_str += f"用户偏好：{'、'.join(user['preferences'])}"
        parts.append(user_str)

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

    from .generic.litellm_adapter import create_litellm_client
    print(f"[Runner] Using LiteLLM adapter for model: {cfg['model']}", file=sys.stderr, flush=True)
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
        category = r.metadata.get("category", "")

        # 对于 MCP 工具，显示完整名称 server/name
        if category == "mcp_tool":
            server = r.metadata.get("server", "")
            display_name = f"{server}/{name}" if server else name
        else:
            display_name = name

        if display_name:
            lines.append(f"{i}. **{display_name}** (分数: {score_pct})")

            # 对于 MCP 工具，组装完整描述（description + input_schema）
            if category == "mcp_tool":
                description = r.metadata.get("description", r.content)
                input_schema = r.metadata.get("input_schema", {})
                if input_schema:
                    # 格式化参数说明
                    props = input_schema.get("properties", {})
                    if props:
                        params = []
                        for param_name, param_info in props.items():
                            param_desc = param_info.get("description", "")
                            param_type = param_info.get("type", "")
                            params.append(f"         - {param_name} ({param_type}): {param_desc}")
                        lines.append(f"       {description}")
                        lines.append(f"       参数:")
                        lines.extend(params)
                    else:
                        lines.append(f"       {description}")
                else:
                    lines.append(f"       {r.content}")
            else:
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
    集成动态注入：Skills 和 MCP 工具描述按语义注入提示词。
    """

    def __init__(self, llm_config: Dict[str, Any], mcp_client=None):
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
        self.tool_lifecycle = ToolLifecycleManager(decay_rate=10, min_score=50)

    def set_mcp_tools_schema(self, tools: list):
        """设置 MCP 工具 Schema（从外部调用）"""
        schema = []
        for tool in tools:
            schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get(
                            "input_schema", {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        self._mcp_tools_schema = schema
        print(f"[NiuRunner] Loaded {len(schema)} MCP tools", file=sys.stderr, flush=True)

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

        # 提取最近5条消息
        recent_messages = history[-5:] if len(history) > 5 else history

        # 拼接内容
        context_parts = []
        for msg in recent_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if content and role in ("user", "assistant"):
                # 截断过长的内容
                if len(content) > 200:
                    content = content[:200] + "..."
                context_parts.append(f"{role}: {content}")

        # 添加当前用户输入
        context_parts.append(f"user: {user_input}")

        return "\n".join(context_parts)

    def _inject_dynamic_resources(self, user_input: str) -> str:
        """
        动态注入相关资源（Skills、MCP 工具描述）

        从向量库搜索 metadata.type in ["skill", "mcp_tool"]
        返回格式化的提示词扩展

        阈值策略：
        - 提高初始阈值到 0.35，过滤掉更多不相关的结果
        - 如果结果太少，降级到文本搜索
        - 结果数量限制保证不会注入过多内容
        """
        # 搜索 Skills（符合L0/L1/L2规范，使用level字段）
        skills = self.vector_search.search(
            query=user_input, limit=3, min_score=0.35, filter={"level": "l1", "category": "skill"}
        )
        print(f"[Debug] Dynamic injection - Skills: {len(skills)} results", file=sys.stderr, flush=True)

        # 搜索 MCP 工具描述（符合L0/L1/L2规范，使用level字段）
        # 降低阈值到 0.15，因为工具描述较短，语义相似度天然偏低
        mcp_tools = self.vector_search.search(
            query=user_input, limit=5, min_score=0.15, filter={"level": "l1", "category": "mcp_tool"}
        )
        print(f"[Debug] Dynamic injection - MCP tools: {len(mcp_tools)} results", file=sys.stderr, flush=True)

        # 搜索知识（符合L0/L1/L2规范，使用level字段）
        knowledge = self.vector_search.search(
            query=user_input,
            limit=8,
            min_score=0.45,  # 提高知识搜索阈值
            filter={"level": "l1", "category": "document"},  # L1 文档摘要
        )
        print(f"[Debug] Dynamic injection - Knowledge: {len(knowledge)} results", file=sys.stderr, flush=True)

        # 搜索 Interaction Habits（用户画像、状态、工具方言）
        interaction_habits = self.vector_search.search_interaction_habits(
            query=user_input, limit=3, min_score=0.4
        )
        print(f"[Debug] Dynamic injection - Interaction Habits: {len(interaction_habits)} results", file=sys.stderr, flush=True)

        # 格式化
        parts = []
        if skills:
            parts.append(format_resources_for_prompt(skills, "相关技能"))
        if mcp_tools:
            parts.append(format_resources_for_prompt(mcp_tools, "可用工具"))
        if knowledge:
            parts.append(format_resources_for_prompt(knowledge, "参考知识"))
        if interaction_habits:
            parts.append(format_resources_for_prompt(interaction_habits, "交互习惯"))

        injection = "\n".join(parts)
        if injection:
            print(f"[Debug] Dynamic injection - Total length: {len(injection)} chars", file=sys.stderr, flush=True)
        else:
            print(f"[Debug] Dynamic injection - Skipped (no relevant results)", file=sys.stderr, flush=True)

        return injection

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
        # 从消息历史中提取上下文（用于向量检索）
        context = self._extract_context_from_history(history, user_input)

        # 动态注入资源
        injection = self._inject_dynamic_resources(context)

        # 组装 system_prompt
        system_prompt = self.base_system_prompt
        if injection:
            system_prompt += injection

        # 组装 tools_schema = 内置工具 + 基础MCP工具
        tools_schema = self.base_tools_schema.copy()

        # 固定注入基础MCP工具（memory-server + vector-store，共11个）
        for tool_name in BASE_MCP_TOOLS:
            schema = self._get_tool_schema_by_name(tool_name)
            if schema:
                tools_schema.append(schema)

        # 动态注入其他工具（基于向量检索）
        # 1. 向量检索工具（使用上下文，而不是单纯的user_input）
        matched_tools = self.vector_search.search(
            query=context,
            limit=3,
            min_score=0.5,
            filter={'category': 'mcp_tool'}
        )

        # 2. 更新工具生命周期（命中工具设置为100分）
        for result in matched_tools:
            tool_name = result.metadata.get('name')
            server = result.metadata.get('server')
            full_name = f"{server}/{tool_name}"
            self.tool_lifecycle.hit_tool(full_name)

        # 3. 获取所有活跃工具（包括命中的 + 之前未衰减完的）
        active_tool_names = self.tool_lifecycle.get_active_tools()

        # 4. 注入活跃工具（排除基础MCP工具，避免重复）
        for tool_name in active_tool_names:
            # 跳过基础MCP工具（已经注入）
            if tool_name in BASE_MCP_TOOLS:
                continue

            schema = self._get_tool_schema_by_name(tool_name)
            if schema:
                tools_schema.append(schema)

        # 调试：打印工具数量
        base_mcp_count = len([t for t in tools_schema if t.get("function", {}).get("name") in BASE_MCP_TOOLS])
        print(
            f"[Debug] tools_schema: {len(self.base_tools_schema)} base + {base_mcp_count} base_mcp = {len(tools_schema)} total (from {len(self._mcp_tools_schema)} available mcp tools)"
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
                    import sys
                    print(f"[ERROR] Failed to extract return_value data: {e}", file=sys.stderr)
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

        # 对话结束后衰减工具分数
        self.tool_lifecycle.decay_tools()

        yield full_resp.strip()


# 全局实例
_runner: Optional[NiuRunner] = None
_runner_lock = threading.Lock()


def get_runner(llm_config: Dict[str, Any] = None, mcp_client=None) -> NiuRunner:
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

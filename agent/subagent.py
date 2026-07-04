"""
SubAgent Module

子 Agent 调用机制。
"""

import os
import re
import json
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from loguru import logger

DEFAULT_CONTEXT_WINDOW_SIZE = 200000
MIN_CONTEXT_WINDOW_SIZE = 32000    # 32K 最小合理值
MAX_CONTEXT_WINDOW_SIZE = 2000000  # 2M 上限

# 默认禁用的基础工具（子 Agent 默认不能调用，需要显式 allowBaseTools 解禁）
# bash 和 grep 是"文件系统乱翻"的元凶，默认禁用
DEFAULT_DISABLED_BASE_TOOLS = {"bash", "grep"}


def _filter_base_tools(agent_config: dict, tools_schema: list) -> tuple:
    """根据 agent_config 的 disableBaseTools/allowBaseTools 过滤基础工具。

    三层过滤逻辑：
    1. 默认黑名单（DEFAULT_DISABLED_BASE_TOOLS，bash/grep 默认禁用）
    2. disableBaseTools 追加禁用
    3. allowBaseTools 从黑名单中解禁（优先级最高）

    Args:
        agent_config: 子 Agent 配置字典（frontmatter 解析结果）
        tools_schema: 待过滤的工具 schema 列表

    Returns:
        (filtered_tools, disabled_set, custom_disabled, allowed_base) 元组：
        - filtered_tools: 过滤后的工具 schema 列表
        - disabled_set: 最终禁用的工具名集合
        - custom_disabled: 子 Agent 自定义 disableBaseTools 列表
        - allowed_base: 子 Agent 自定义 allowBaseTools 列表
    """
    disabled_set = set(DEFAULT_DISABLED_BASE_TOOLS)
    custom_disabled = agent_config.get("disableBaseTools", [])
    if custom_disabled:
        disabled_set |= set(custom_disabled)
    allowed_base = agent_config.get("allowBaseTools", [])
    if allowed_base:
        disabled_set -= set(allowed_base)

    if disabled_set:
        filtered = [
            t for t in tools_schema
            if t.get("function", {}).get("name", "") not in disabled_set
        ]
    else:
        filtered = list(tools_schema)

    return filtered, disabled_set, custom_disabled, allowed_base


# 子 Agent 职责边界段模板（自动注入到正文未含"直接退出"语义的子 Agent）
_BOUNDARY_SECTION_TEMPLATE = """## 职责边界

你的职责范围由上方系统提示词界定的功能描述决定。
不要猜测含义，无法完全确认属于自己的职责范围的，就要直接退出，回复主 Agent。"""


def count_tokens_for_text(text: str) -> int:
    """
    计算文本的 token 数量（用于子 Agent prompt 分片判断）

    使用 TokenCalculator，回退到字符数估算。

    Args:
        text: 纯文本字符串

    Returns:
        token 数量
    """
    if not text:
        return 0
    try:
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_text(text)
    except Exception:
        # 回退：约 2 字符/token（偏保守）
        return max(1, len(text) // 2)


def _get_user_config_path() -> Path:
    """Locate config/user-config.json relative to project root."""
    return Path(__file__).parent.parent / "config" / "user-config.json"


def _read_context_window_tokens() -> int:
    """Read context window size from config/user-config.json."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        size = config.get("context", {}).get("contextWindowSize", DEFAULT_CONTEXT_WINDOW_SIZE)
        if isinstance(size, (int, float)) and MIN_CONTEXT_WINDOW_SIZE <= size <= MAX_CONTEXT_WINDOW_SIZE:
            return int(size)
        logger.warning(f"Invalid contextWindowSize {size}, using default {DEFAULT_CONTEXT_WINDOW_SIZE}")
    except Exception:
        pass
    return DEFAULT_CONTEXT_WINDOW_SIZE


def _read_context_threshold(key: str, default: float) -> float:
    """Read a context threshold from config/user-config.json.

    Args:
        key: Field name in context section (e.g. 'warningThreshold', 'targetThreshold')
        default: Default value if key not found or invalid
    """
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get(key, default)
        if isinstance(val, (int, float)) and 0.0 < val < 1.0:
            return float(val)
    except Exception:
        pass
    return default


def _read_warning_threshold() -> float:
    """Read warning threshold (overflow detection). Default 0.80, matching Rust launcher."""
    return _read_context_threshold("warningThreshold", 0.80)


def _read_target_threshold() -> float:
    """Read target threshold (compress target usage). Default 0.30 — compress to 30% of window to reduce forced compression frequency."""
    return _read_context_threshold("targetThreshold", 0.30)


DEFAULT_PROTECT_RECENT_COUNT = 10


def _read_protect_recent_count() -> int:
    """Read protectRecentCount from config/user-config.json. Default 10."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get("protectRecentCount", DEFAULT_PROTECT_RECENT_COUNT)
        if isinstance(val, int) and val >= 0:
            return val
    except Exception:
        pass
    return DEFAULT_PROTECT_RECENT_COUNT


DEFAULT_COMPRESS_TARGET_TOKENS = 60000
MAX_OUTPUT_TOKENS_RATIO = 0.16  # contextWindowSize × 0.16
MAX_OUTPUT_TOKENS_CAP = 65536   # 封顶 65536


def _read_compress_target_tokens() -> int:
    """Read compressTargetTokens from config/user-config.json. Default 60000."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get("compressTargetTokens", DEFAULT_COMPRESS_TARGET_TOKENS)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return int(val)
        logger.warning(f"Invalid compressTargetTokens {val}, using default {DEFAULT_COMPRESS_TARGET_TOKENS}")
    except Exception:
        pass
    return DEFAULT_COMPRESS_TARGET_TOKENS


def _read_max_output_tokens() -> int:
    """动态计算 max_output_tokens：contextWindowSize × 0.16，封顶 65536。

    不读配置 maxOutputTokens（已删除硬编码）。
    换模型自动适配：不同模型 contextWindowSize 不同，×0.16 自动算对应值。
    200K → 32000；128K → 20480；400K → 64000（封顶前）；500K → 65536（封顶）。
    """
    context_window = _read_context_window_tokens()
    val = int(context_window * MAX_OUTPUT_TOKENS_RATIO)
    return min(val, MAX_OUTPUT_TOKENS_CAP)


def _run_agent_loop(
    client,
    system_prompt: str = "",  # 向后兼容（system_message 非 None 时优先）
    system_message: Optional[dict] = None,  # 已组装好的 system message（首轮即带 cache_control）
    user_input: str = "",
    handler=None,
    tools_schema: list = None,
    max_turns: int = 20,
    initial_user_content: Optional[str] = None,
    context_window_tokens: int = 0,
    context_fifo_threshold: int = 0,
    context_target_threshold: int = 0,
    history: Optional[list] = None,
    supplement_queue: Optional[Any] = None,  # 子 Agent 独立 supplement queue
    memory_context: Optional[Any] = None,  # 阶段二新增：异步子 Agent 进度数据
) -> Tuple[str, Any]:
    """
    执行 agent_runner_loop 并收集结果（提取自 call_subagent）

    Args:
        agent_name: 子 Agent 名称（用于日志）
        client: LLM 客户端
        system_prompt: 系统提示词（向后兼容，system_message 非 None 时优先）
        system_message: 已组装好的 system message（首轮即带 cache_control）
        user_input: 用户输入
        handler: NiuHandler 实例
        tools_schema: 工具 schema 列表
        max_turns: 最大轮次
        initial_user_content: 初始用户内容（如果不提供则使用 user_input）
        context_window_tokens: 上下文窗口 token 数（0 表示不检查）

    Returns:
        (result_text, return_value) 元组
    """
    from .generic.agent_loop import agent_runner_loop, StreamEvent

    if initial_user_content is None:
        initial_user_content = user_input

    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        system_message=system_message,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        verbose=False,
        initial_user_content=initial_user_content,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
        context_target_threshold=context_target_threshold,
        on_context_high_usage=None,
        history=history,
        enable_supplement=True,  # 子 Agent 用独立 supplement queue
        supplement_drain=supplement_queue.drain if supplement_queue is not None else None,
        memory_context=memory_context,  # 阶段二新增：透传给 agent_runner_loop
    )

    result = ""
    return_value = None

    while True:
        # 子 Agent 不再检查全局 stop 信号灯，只响应自己 queue 的 /stop
        try:
            chunk = next(gen)
            if isinstance(chunk, str):
                result += chunk
            elif isinstance(chunk, StreamEvent):
                if chunk.type == "reply":
                    result += chunk.content
                # 忽略 persist/system/tool_marker — 这些是子Agent内部过程，不应返回给主Agent
            else:
                logger.warning(f"[SubAgent] Non-string chunk from agent_runner_loop: {type(chunk).__name__}")
        except StopIteration as e:
            return_value = e.value
            break

    return result, return_value


def _extract_result_from_return_value(return_value: Any) -> Optional[str]:
    """
    从 agent_runner_loop 的 return 值中提取结构化结果文本

    控制流 dict（如 CONTEXT_OVERFLOW, EXITED, MAX_TURNS_EXCEEDED, CURRENT_TASK_DONE, TERMINATED_BY_SUPPLEMENT）
    不应被序列化为结果文本，应返回 None 让调用者回退到 result_text。

    Args:
        return_value: agent_runner_loop 的 StopIteration.value

    Returns:
        提取的结果字符串，如果无法提取则返回 None
    """
    if return_value and isinstance(return_value, dict):
        # 控制流 dict 不应被序列化为结果 — 返回 None
        control_flow_results = {"CONTEXT_OVERFLOW", "EXITED", "MAX_TURNS_EXCEEDED", "CURRENT_TASK_DONE", "TERMINATED_BY_SUPPLEMENT"}
        if return_value.get("result") in control_flow_results:
            return None

        if "data" in return_value and return_value["data"] is not None:
            data = return_value["data"]
            if isinstance(data, dict):
                return json.dumps(data, ensure_ascii=False)
            return json.dumps(data, ensure_ascii=False, default=str)
        return json.dumps(return_value, ensure_ascii=False)
    return None


# kebab-case 校验正则（小写字母/数字/连字符，且不以连字符开头/结尾）
_KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 模块级常量（测试时可 patch，避免依赖 __file__ 计算路径）
_PROJECT_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "agents"
)
_USER_AGENTS_DIR = os.path.join(os.path.expanduser("~/.niu/agents"))


def _resolve_agent_md_path(agent_name: str) -> Optional[str]:
    """查找子 Agent MD 文件路径。

    先查项目目录 config/agents/{name}.md（专用子 Agent 优先），
    再查用户目录 ~/.niu/agents/{name}.md（通用子 Agent）。

    Args:
        agent_name: 子 Agent 名称（如 file-processor、photo-organizer）

    Returns:
        找到则返回绝对路径，找不到返回 None。
        agent_name 非 kebab-case 时返回 None（防御路径穿越）。
    """
    # 防御深度：agent_name 非 kebab-case 时拒绝（防 ../ 路径穿越）
    if not _KEBAB_CASE_RE.match(agent_name):
        return None

    # 项目目录（专用子 Agent）
    project_path = os.path.join(_PROJECT_AGENTS_DIR, f"{agent_name}.md")
    if os.path.exists(project_path):
        return project_path

    # 用户目录（通用子 Agent，主 Agent 运行时创建）
    user_path = os.path.join(_USER_AGENTS_DIR, f"{agent_name}.md")
    if os.path.exists(user_path):
        return user_path

    return None


def get_subagent_config(agent_name: str) -> Dict[str, Any]:
    """
    获取子 Agent 配置

    Args:
        agent_name: 子 Agent 名称（如 file-processor、photo-organizer）

    Returns:
        配置字典，包含 mcpServers 等字段。MD 文件不存在时返回空 dict。
    """
    prompt_path = _resolve_agent_md_path(agent_name)

    if prompt_path and os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 解析 YAML front matter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        config = yaml.safe_load(parts[1])
                        if config:
                            return config
                    except Exception:
                        pass

    return {}


def get_subagent_prompt(agent_name: str) -> str:
    """获取子 Agent 提示词"""
    prompt_path = _resolve_agent_md_path(agent_name)

    if prompt_path and os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 提取 body（--- 后面的内容）
            if "---" in content:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content

    return f"You are {agent_name} sub-agent. Complete the task efficiently."


def build_subagent_system_segments(agent_name: str) -> tuple:
    """构建子 Agent 的静态/动态系统提示词段（cache 友好）。

    Args:
        agent_name: 子 Agent 名

    Returns:
        (static_system, dynamic_system):
        - static_system: agent.md 正文 + user_info_section（字节稳定，cache 前缀）
        - dynamic_system: Current Time（每分钟变化，不 cache）
    """
    # 1. 获取子 Agent 提示词（从配置文件）
    static_system = get_subagent_prompt(agent_name)

    # 2. 注入用户信息和偏好（静态段，子 Agent 需要了解用户背景）
    user_info_section = _build_user_info_section()
    if user_info_section:
        static_system += "\n\n" + user_info_section

    # 3. 注入职责边界段（如果子 Agent 正文未含"直接退出"语义，自动追加通用模板）
    #    按语义关键词检测而非标题，避免 dream-evolver 已有"## 职责边界"段（职责声明）
    #    但不含退出语义时被误跳过
    if "直接退出" not in static_system:
        static_system += "\n\n" + _BOUNDARY_SECTION_TEMPLATE

    # 4. 动态段：Current Time
    from datetime import datetime
    now = datetime.now()
    dynamic_system = f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

    return static_system, dynamic_system


def get_subagent_mcp_tools_schema(agent_name: str) -> List[Dict]:
    """
    获取子 Agent 的 MCP 工具 schema

    根据子 Agent 配置中的 mcpServers 过滤工具，支持 mcpToolFilter 白名单

    Args:
        agent_name: 子 Agent 名称

    Returns:
        MCP 工具 schema 列表（OpenAI格式）
    """
    from .tool_registry import get_registry

    config = get_subagent_config(agent_name)
    mcp_servers = config.get("mcpServers", [])
    mcp_tool_filter = config.get("mcpToolFilter", {})

    if not mcp_servers:
        return []

    # 从 ToolRegistry 获取所有工具
    registry = get_registry()
    all_tools = registry.get_schemas()

    # 过滤出指定服务器的工具，并转换为OpenAI格式
    schema = []
    for tool in all_tools:
        tool_name = tool.get("name", "")
        # 工具名格式：server_name/tool_name
        if "/" in tool_name:
            server = tool_name.split("/")[0]
            bare_name = tool_name.split("/", 1)[1]
            if server in mcp_servers:
                # 如果该服务器有白名单，只注入白名单中的工具
                server_filter = mcp_tool_filter.get(server)
                if server_filter is not None and bare_name not in server_filter:
                    continue
                # hidden 只对主 Agent 生效；子 Agent 由 mcpServers 白名单控制工具范围
                # 转换为OpenAI工具格式
                schema.append({
                    "type": "function",
                    "function": {
                        "name": bare_name,  # LLM sees bare name; handler auto-resolves to full name
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    }
                })

    logger.info(f"[SubAgent] {agent_name}: Found {len(schema)} MCP tools for servers {mcp_servers}")
    return schema


def _build_user_info_section() -> str:
    """从 memory.json 构建 ## 用户信息 + ## 用户偏好 段落，供子Agent注入。

    过滤规则：
    - user 字段：值以"请询问"开头则跳过
    - permanent 字段：只注入 type="memory"，过滤 type="task"
    """
    from pathlib import Path

    memory_path = Path.home() / ".niu" / "memory.json"
    if not memory_path.exists():
        return ""

    try:
        import json
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    sections = []

    # 工作目录
    workspace = memory.get("workspace", {})
    ws_path = workspace.get("path", "")
    if ws_path and not str(ws_path).startswith("请询问"):
        sections.append(f"## 工作目录\n\n{ws_path}")

    # 用户信息：子Agent不需要用户身份（姓名/称呼/职业/工作单位），只需工作目录和偏好

    # 用户偏好（仅 type="memory"）
    permanent = memory.get("permanent", [])
    memory_items = [item for item in permanent if item.get("type") == "memory" and item.get("content")]
    if memory_items:
        pref_lines = [f"{i}. {item['content']}" for i, item in enumerate(memory_items, 1)]
        sections.append("## 用户偏好\n\n" + "\n".join(pref_lines))

    return "\n\n".join(sections)


def _build_subagent_tools_schema(
    agent_name: str,
    agent_config: Optional[dict] = None,
    memory_context: Optional[Any] = None,
    no_tools: bool = False,
) -> list:
    """构建子 Agent 的 tools_schema。

    阶段二新增：异步子 Agent（memory_context 非 None）注入 ask_main_agent 工具。
    同步子 Agent（memory_context None）不注入（避免死锁）。

    Args:
        agent_name: 子 Agent 名（如 file-processor）
        agent_config: 子 Agent 配置字典（frontmatter 解析结果）。None 时内部调 get_subagent_config 获取
        memory_context: 非 None 表示异步子 Agent，注入 ask_main_agent；None 表示同步
        no_tools: True 时返回空列表（强制无工具模式）

    Returns:
        tools_schema 列表
    """
    if no_tools:
        # 注意：现有 call_subagent 在最后清空 tools_schema，日志走完过滤流程才清空
        # helper 提前返回跳过日志，但功能结果一致（都返回空）。可接受——no_tools 模式不需要日志
        return []

    # agent_config None 时内部获取（方便测试只传 agent_name + memory_context）
    if agent_config is None:
        agent_config = get_subagent_config(agent_name)

    from .runner import get_tools_schema

    tools_schema = get_tools_schema(include_main_only=False)
    # 移除 chat-with-* 工具，子 Agent 不能再调用子 Agent
    tools_schema = [
        t for t in tools_schema
        if not t.get("function", {}).get("name", "").startswith("chat-with-")
    ]
    # 三层过滤：默认黑名单 + disableBaseTools + allowBaseTools 解禁
    tools_schema, disabled_set, custom_disabled, allowed_base = _filter_base_tools(agent_config, tools_schema)
    if disabled_set:
        logger.info(f"[SubAgent] {agent_name}: Disabled base tools: {sorted(disabled_set)}")

    # 配置完整性检查
    if not custom_disabled and not allowed_base:
        logger.warning(
            f"[SubAgent] {agent_name}: No disableBaseTools/allowBaseTools configured, "
            f"using default blacklist only: {sorted(DEFAULT_DISABLED_BASE_TOOLS)}."
        )

    # MCP 工具
    mcp_tools_schema = get_subagent_mcp_tools_schema(agent_name)
    if mcp_tools_schema:
        tools_schema = tools_schema + mcp_tools_schema
        logger.info(f"[SubAgent] {agent_name}: {len(tools_schema)} tools ({len(mcp_tools_schema)} MCP)")
    else:
        logger.warning(f"[SubAgent] {agent_name}: {len(tools_schema)} tools (0 MCP - WARNING: No MCP tools loaded!)")

    # 阶段二：异步子 Agent 注入 ask_main_agent
    if memory_context is not None:
        tools_schema.append(ASK_MAIN_AGENT_TOOL_SCHEMA)
        logger.info(f"[SubAgent] {agent_name}: ask_main_agent 注入（异步子 Agent）")

    # 列出关键工具（调试）
    tool_names = [t.get("function", {}).get("name", "") for t in tools_schema]
    logger.debug(f"[SubAgent] {agent_name}: Tools = {tool_names}")

    return tools_schema


def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
    context_fifo_threshold: int = -1,
    no_tools: bool = False,
    supplement_queue: Optional[Any] = None,
    memory_context: Optional[Any] = None,  # 阶段二新增：异步子 Agent 进度数据
    unique_name: Optional[str] = None,  # 阶段二新增：异步路径透传，跳过内部 register
) -> str:
    """
    调用子 Agent

    子 Agent 是独立的临时 session，干完就消失。
    不需要动态注入，直接使用配置文件中的提示词和工具。

    Args:
        agent_name: 子 Agent 名称（如 file-processor）
        task: 任务描述
        llm_config: LLM 配置
        mcp_client: MCP 客户端
        history: 历史消息
        context_fifo_threshold: FIFO 截断阈值。-1 = 默认 75%，0 = 关闭 FIFO，>0 = 自定义值
        no_tools: 禁用所有工具（LLM 只能直接回复文本，不能调用任何工具）

    Returns:
        子 Agent 执行结果
    """
    from .handler import NiuHandler

    # 1. 获取子 Agent 提示词 + temperature
    agent_config = get_subagent_config(agent_name)
    if agent_config.get("temperature") is not None:
        llm_config = {**llm_config, "temperature": agent_config["temperature"]}

    # 2. 构建静态/动态段（cache 友好）
    static_system, dynamic_system = build_subagent_system_segments(agent_name)

    # 3. 组装 system message（按 model 决定格式：Claude list / 其他 str）
    model_lower = (llm_config.get("model", "") or "").lower()
    if "claude" in model_lower:
        system_message = {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_system,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": dynamic_system},
            ],
        }
    else:
        system_message = {
            "role": "system",
            "content": static_system + dynamic_system,
        }

    # 3. 创建 LLM 客户端（统一使用 LiteLLM）
    from .runner import create_client
    client = create_client(llm_config)

    # 4. 创建 handler（禁用记忆检索，子 Agent 不需要）
    handler = NiuHandler(mcp_client=mcp_client)
    handler._disable_memory_recall = True
    # 重要约定：子 Agent 必须标记 _is_subagent = True
    # 否则子 Agent 的工具调用会触发 brain region reinforcement（应只由主 Agent 触发）
    # 新增子 Agent 时必须遵守此约定
    handler._is_subagent = True

    # 5. 阶段二：tools_schema 构建提取到 helper（含 ask_main_agent 注入逻辑）
    tools_schema = _build_subagent_tools_schema(
        agent_name=agent_name,
        agent_config=agent_config,
        memory_context=memory_context,
        no_tools=no_tools,
    )

    # 7. 执行（单次，不分片）
    context_window_tokens = _read_context_window_tokens()
    # FIFO 截断阈值：-1 = 默认 75%，0 = 关闭 FIFO，>0 = 自定义值
    if context_fifo_threshold == -1:
        fifo_threshold = int(context_window_tokens * 0.75)
    elif context_fifo_threshold == 0:
        fifo_threshold = 0
    else:
        fifo_threshold = context_fifo_threshold

    # FIFO 裁剪目标 token 量
    target_threshold = _read_target_threshold()
    context_target_threshold_val = int(context_window_tokens * target_threshold) if context_window_tokens > 0 else 0

    # === 创建 supplement queue + 注册到 SubagentRegistry ===
    from .subagent_supplement import SubagentSupplementQueue
    from .subagent_registry import SubagentRegistry

    if unique_name is not None:
        # 异步路径：调用方已注册（_dispatch_async_subagent），跳过内部 register
        # 只设置 handler._subagent_unique_name（handler.dispatch 的 ask_main_agent 分支用）
        handler._subagent_unique_name = unique_name
        # supplement_queue 也由调用方传入，不重新创建
        try:
            result_text, return_value = _run_agent_loop(
                client=client,
                system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=20,
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,  # 调用方传入
                memory_context=memory_context,  # 阶段二新增：透传给 _run_agent_loop
            )
        finally:
            # 异步路径不在这里 unregister（_run_subagent_async 的 finally 负责）
            pass
    else:
        # 同步路径：现有逻辑
        if supplement_queue is None:
            supplement_queue = SubagentSupplementQueue(unique_name="")  # unique_name 注册后回填
        unique_name = SubagentRegistry.register(agent_name, supplement_queue)
        supplement_queue.unique_name = unique_name  # 回填唯一名，db 监测程序路由时用
        # 同步路径也设 handler._subagent_unique_name（虽然同步子 Agent 不注入 ask_main_agent，
        # 但设上无副作用，且未来若误注入也能优雅报错而非 AttributeError）
        handler._subagent_unique_name = unique_name
        try:
            result_text, return_value = _run_agent_loop(
                client=client,
                system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=20,
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,  # 新增：传给 _run_agent_loop
                memory_context=memory_context,  # 阶段二新增：透传给 _run_agent_loop
            )
        finally:
            SubagentRegistry.unregister(unique_name)

    # 检测输出截断（finish_reason == "length"）
    # LLM 输出被截断时无法产出合法 keep/update 结构，返回字符串信号让降级循环识别
    if return_value and isinstance(return_value, dict):
        if return_value.get("finish_reason") == "length":
            logger.warning(f"[SubAgent] {agent_name}: Output truncated (finish_reason=length)")
            return "COMPACT_TRUNCATED"

    # CONTEXT_OVERFLOW：返回结构化进度报告
    if return_value and isinstance(return_value, dict) and return_value.get("result") == "CONTEXT_OVERFLOW":
        data = return_value.get("data", {})
        overflow_report = {
            "overflow": True,
            "agent": agent_name,
            "turns_completed": data.get("turns_completed", 0),
            "tokens_used": data.get("tokens_used", 0),
            "tokens_limit": data.get("tokens_limit", 0),
            "partial_result": result_text[-8000:] if result_text else "",
        }
        logger.warning(f"[SubAgent] {agent_name}: Context overflow at {data.get('tokens_used', 0)} tokens")
        return json.dumps(overflow_report, ensure_ascii=False)

    # 优先从 return 值提取结构化结果
    extracted = _extract_result_from_return_value(return_value)
    if extracted is not None:
        return extracted

    return result_text


# ==================== 阶段二：ask_main_agent 工具 ====================

# ask_main_agent 工具 schema（注入给异步子 Agent）
ASK_MAIN_AGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_main_agent",
        "description": (
            "向主 Agent 提问并阻塞等待回答。当遇到歧义、需要澄清或需要主 Agent 决策时使用。"
            "调用后会阻塞直到主 Agent 回答（通过 db_monitor 路由）。"
            "不要在主 Agent 没回答前连续调用多次——一次只问一个问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问主 Agent 的问题，描述清楚歧义点。",
                },
            },
            "required": ["question"],
        },
    },
}


def _ask_main_agent_impl(question: str, unique_name: str) -> str:
    """ask_main_agent 工具实现。

    子 Agent 调用流程（阶段二内存队列机制）：
      1. 检查是否已被 cancel 过（_ask_terminated 标记）——避免 cancel 后 LLM 又调 ask_main_agent 死锁
      2. 注册 future 到 PendingAskRegistry（key=unique_name）
      3. 推 "[unique_name] question" 到 MainAgentRequestQueue 内存队列（**不写 db**）
      4. future.wait() 阻塞（不写 db，由 db_monitor 检测主 Agent 闲置时推 SSE 触发前端）
      5. 前端调 /api/chat/session → 后端写 user 消息到 db + 调 LLM → 主 Agent 回复 @子名 回答
      6. db_monitor 链路 B 轮询到主 Agent 回复的 subagent_msg → set_answer 解除 future
      7. 返回回答文本；如果被 cancel（主 Agent 发 /stop）返回 terminated 状态 + 设置 _ask_terminated 标记

    关键：消息不写 db，进内存队列。db_monitor 检测主 Agent 闲置时推 SSE 触发前端，
    前端调 /api/chat/session 后由后端 compat.py 写 user 消息到 db（作为最后一条 user 消息，
    LLM 才会作为当前输入处理）。

    Args:
        question: 子 Agent 要问的问题
        unique_name: 子 Agent 唯一名（注册 future 用）

    Returns:
        主 Agent 的回答文本，或 terminated 状态提示
    """
    from .ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
    from .main_agent_request_queue import get_main_agent_request_queue
    from .subagent_registry import SubagentRegistry

    registry = get_pending_ask_registry()

    # 阶段二防死锁检查：如果该子 Agent 之前已被 cancel（_ask_terminated 标记），
    # 直接返回 terminated 状态，不再注册 future 阻塞
    instance = SubagentRegistry.get(unique_name)
    if instance is None:
        # Important-1 修复：子 Agent 已注销（异常路径），不再 register future
        # cancel 信号无法设置 _ask_terminated 标记，future 会永远等不到 answer
        return "[ask_main_agent 错误] 子 Agent 已不在注册表（可能已被停止或退出），无法询问主 Agent。"

    if getattr(instance, "_ask_terminated", False):
        return "[ask_main_agent 已终止] 主 Agent 已发出停止指令，请总结本轮工作后终止。"

    future = registry.register(unique_name)

    # 推入 MainAgentRequestQueue 内存队列（不写 db）
    # content 格式 "[子名] 问题"——db_monitor 推 SSE 时 role=subagent_msg，
    # 前端收到后调 /api/chat/session，content 作为 message 参数传给后端，
    # 后端 compat.py 写 user 消息（role=user, content="[子名] 问题"）
    #
    # 阶段二 E1：question sanitization
    # - 长度限制 2000 字符（避免恶意子 Agent 把超大内容塞进队列）
    # - strip 行首 @ 字符（避免被 at_message_parser 误解析为 @消息注入指令）
    sanitized_question = question[:2000] if question else ""
    if sanitized_question.lstrip().startswith("@"):
        sanitized_question = sanitized_question.lstrip()[1:]
    msg_content = f"[{unique_name}] {sanitized_question}"
    try:
        get_main_agent_request_queue().push(msg_content)
    except Exception as e:
        # 推队列失败 → 注销 future，返回错误
        registry.unregister(unique_name)
        return f"[ask_main_agent 错误] 推入 MainAgentRequestQueue 失败：{e}"

    # 阻塞等待（加超时避免 db_monitor 崩溃时子 Agent 永久阻塞）
    # 超时 300 秒（5 分钟）——主 Agent 可能忙很久，5 分钟够用
    # 超时返回提示让子 Agent 自行决策；被 cancel 返回 terminated 状态
    answer = future.wait(timeout=300)

    if answer == TERMINATED_SIGNAL:
        # 主 Agent 发 /stop，工具识别后返回终止状态
        # 设置 _ask_terminated 标记到 SubagentRegistry 实例，防止 LLM 再次调 ask_main_agent 死锁
        if instance is not None:
            instance._ask_terminated = True
        return "[ask_main_agent 已终止] 主 Agent 已发出停止指令，请总结本轮工作后终止。"

    if answer is None:
        # 超时（5 分钟主 Agent 未回答）——返回决策提示让子 Agent 自己决定
        # 不强制退出，让子 Agent 根据任务情况选择重新问 or 跳过继续
        # 注销 future 避免泄漏
        registry.unregister(unique_name)
        logger.warning(f"ask_main_agent 超时（5 分钟无回答），unique_name={unique_name}")
        return (
            "[ask_main_agent 超时] 主 Agent 5 分钟内未响应。你可以：\n"
            "1. 重新调用 ask_main_agent 再问一次（主 Agent 可能刚才在忙）\n"
            "2. 跳过这个问题，基于现有信息继续工作（如果这个回答不是必须的）\n"
            "请根据当前任务情况决定。"
        )

    return answer


# ==================== 阶段二：异步子 Agent 派发与运行 ====================


def _dispatch_async_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
) -> str:
    """异步派子 Agent：立即返回派单确认，子 Agent 在后台 asyncio 协程跑（跨线程用 run_coroutine_threadsafe 提交到主 loop）。

    流程：
      1. 创建 supplement_queue + memory_context
      2. 注册到 SubagentRegistry（is_sync=False）
      3. run_coroutine_threadsafe(_run_subagent_async(...), loop) 跨线程提交到主 loop
      4. 立即返回派单确认（含唯一名 + 使用说明）

    Returns:
        派单确认文本（含唯一名 + 使用说明）
    """
    import asyncio
    from .subagent_supplement import SubagentSupplementQueue
    from .subagent_memory import SubagentMemoryContext
    from .subagent_registry import SubagentRegistry

    # 创建 supplement_queue + memory_context
    sq = SubagentSupplementQueue(unique_name="")  # unique_name 注册后回填
    mc = SubagentMemoryContext()

    # 注册（is_sync=False，task 稍后回填——run_coroutine_threadsafe 需要主 loop 在跑）
    unique_name = SubagentRegistry.register(
        agent_type=agent_name,
        supplement_queue=sq,
        memory_context=mc,
        is_sync=False,
        task=None,  # 占位，run_coroutine_threadsafe 后回填
    )
    sq.unique_name = unique_name  # 回填唯一名

    # 启动 asyncio task（主 Agent 在 executor 线程跑，必须用 run_coroutine_threadsafe 跨线程调度到主 loop）
    from niu_api.chat import _main_loop
    loop = _main_loop
    if loop is None or loop.is_closed():
        SubagentRegistry.unregister(unique_name)
        return "[错误] 主 asyncio loop 不可用，无法派发异步子 Agent"

    # 用 run_coroutine_threadsafe 跨线程调度（handler.dispatch 在 executor 线程，不在主 loop）
    try:
        future = asyncio.run_coroutine_threadsafe(
            _run_subagent_async(
                unique_name=unique_name,
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=mcp_client,
                memory_context=mc,
                supplement_queue=sq,
            ),
            loop,
        )
    except Exception as e:
        # run_coroutine_threadsafe 失败 → 注销子 Agent，避免残留 task=None 的泄漏
        SubagentRegistry.unregister(unique_name)
        logger.error(f"[AsyncSubagent] 派发失败：{e}")
        return f"[错误] 派发异步子 Agent 失败：{e}"

    # 回填 future 到注册表（用 future 而非 asyncio.Task，因为跨线程调度返回的是 concurrent.futures.Future）
    instance = SubagentRegistry.get(unique_name)
    if instance is not None:
        instance.task = future

    logger.info(f"[AsyncSubagent] 已派出异步子 Agent：{unique_name}")

    return (
        f"已派出子 Agent {unique_name}（类型：{agent_name}），后台运行中。\n"
        f"你可以用 check_subagent_progress('{unique_name}') 查看进度，\n"
        f"写 @ {unique_name} 消息给它补充上下文，\n"
        f"写 @ {unique_name} /stop 停止它。"
    )


async def _run_subagent_async(
    unique_name: str,
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    memory_context,
    supplement_queue,
    mcp_client=None,
) -> None:
    """异步子 Agent 的 asyncio task 主体。

    跑在 asyncio.to_thread 独立线程（call_subagent 是同步函数），主 loop 不阻塞。
    完成后推 [子名] 已完成 到 MainAgentRequestQueue（不写 db）。
    异常或终止时推对应通知。
    最后从 SubagentRegistry 注销 + 清理 PendingAskRegistry。
    """
    import asyncio
    from .ask_main_agent import get_pending_ask_registry
    from .main_agent_request_queue import get_main_agent_request_queue
    from .subagent_registry import SubagentRegistry

    try:
        # call_subagent 是同步函数，用 asyncio.to_thread 包一层避免阻塞主 loop
        # 阶段二关键：传 unique_name=unique_name，跳过 call_subagent 内部 register
        # （_dispatch_async_subagent 已注册过，避免双重注册 + handler._subagent_unique_name 不匹配）
        result = await asyncio.to_thread(
            call_subagent,
            agent_name=agent_name,
            task=task,
            llm_config=llm_config,
            mcp_client=mcp_client,
            history=None,
            supplement_queue=supplement_queue,
            memory_context=memory_context,
            unique_name=unique_name,  # 透传 unique_name，跳过 call_subagent 内部 register
        )

        # 推完成通知到 MainAgentRequestQueue 内存队列（不写 db）
        # content 格式 "[子名] 已完成，结果：..."——db_monitor 检测主 Agent 闲置时推 SSE 触发前端
        completion_msg = f"[{unique_name}] 已完成，结果：{result[:2000]}"
        try:
            get_main_agent_request_queue().push(completion_msg)
        except Exception as e:
            logger.error(f"[AsyncSubagent] {unique_name} 推完成通知失败：{e}")

        logger.info(f"[AsyncSubagent] {unique_name} 完成")

    except Exception as e:
        # 异常通知也推入 MainAgentRequestQueue
        err_msg = f"[{unique_name}] 异常结束：{str(e)[:1000]}"
        try:
            get_main_agent_request_queue().push(err_msg)
        except Exception:
            pass
        logger.error(f"[AsyncSubagent] {unique_name} 异常：{e}")

    except asyncio.CancelledError:
        # 阶段二 B2：应用关闭 / task 被 cancel 时，asyncio.CancelledError 是 BaseException
        # 子类（Python 3.8+），不会被 except Exception 捕获。如果不处理，主 Agent 不知道
        # 子 Agent 被取消。这里推一条取消通知到 MainAgentRequestQueue 让主 Agent 知晓，
        # 再 raise 让上层（run_coroutine_threadsafe 的 future）感知到取消。
        cancel_msg = f"[{unique_name}] 被取消（应用关闭或主 Agent 停止）"
        try:
            get_main_agent_request_queue().push(cancel_msg)
        except Exception:
            pass
        logger.info(f"[AsyncSubagent] {unique_name} 被 cancel")
        raise  # 重新抛出 CancelledError

    finally:
        # 清理 ask_main_agent pending future（避免泄漏）
        get_pending_ask_registry().unregister(unique_name)
        # 从注册表注销
        SubagentRegistry.unregister(unique_name)

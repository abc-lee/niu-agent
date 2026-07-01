"""
SubAgent Module

子 Agent 调用机制。
"""

import os
import json
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from loguru import logger

DEFAULT_CONTEXT_WINDOW_SIZE = 200000
MIN_CONTEXT_WINDOW_SIZE = 32000    # 32K 最小合理值
MAX_CONTEXT_WINDOW_SIZE = 2000000  # 2M 上限


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
    from .runner import is_stop_requested

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
        enable_supplement=False,
    )

    result = ""
    return_value = None

    while True:
        # 协作式停止：每次迭代检查，发现停止立即退出
        if is_stop_requested():
            logger.info("[SubAgent] Stop requested, exiting loop")
            # 不调用 clear_stop()，让主Agent也能检测到停止标志
            break
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

    控制流 dict（如 CONTEXT_OVERFLOW, EXITED, MAX_TURNS_EXCEEDED, CURRENT_TASK_DONE）
    不应被序列化为结果文本，应返回 None 让调用者回退到 result_text。

    Args:
        return_value: agent_runner_loop 的 StopIteration.value

    Returns:
        提取的结果字符串，如果无法提取则返回 None
    """
    if return_value and isinstance(return_value, dict):
        # 控制流 dict 不应被序列化为结果 — 返回 None
        control_flow_results = {"CONTEXT_OVERFLOW", "EXITED", "MAX_TURNS_EXCEEDED", "CURRENT_TASK_DONE"}
        if return_value.get("result") in control_flow_results:
            return None

        if "data" in return_value and return_value["data"] is not None:
            data = return_value["data"]
            if isinstance(data, dict):
                return json.dumps(data, ensure_ascii=False)
            return json.dumps(data, ensure_ascii=False, default=str)
        return json.dumps(return_value, ensure_ascii=False)
    return None


def get_subagent_config(agent_name: str) -> Dict[str, Any]:
    """
    获取子 Agent 配置

    Args:
        agent_name: 子 Agent 名称（如 file-processor）

    Returns:
        配置字典，包含 mcpServers 等字段
    """
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "agents", f"{agent_name}.md"
    )

    if os.path.exists(prompt_path):
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
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "agents", f"{agent_name}.md"
    )

    if os.path.exists(prompt_path):
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

    # 3. 动态段：Current Time
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


def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
    context_fifo_threshold: int = -1,
    no_tools: bool = False,
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

    # 5. 获取基础工具 schema（排除子Agent调用工具，避免递归）
    from .runner import get_tools_schema

    tools_schema = get_tools_schema()
    # 移除 chat-with-* 工具，子Agent不能再调用子Agent
    tools_schema = [
        t for t in tools_schema
        if not t.get("function", {}).get("name", "").startswith("chat-with-")
    ]
    # 根据 disableBaseTools 配置移除基础工具
    disabled_base = agent_config.get("disableBaseTools", [])
    if disabled_base:
        tools_schema = [
            t for t in tools_schema
            if t.get("function", {}).get("name", "") not in disabled_base
        ]
        logger.info(f"[SubAgent] {agent_name}: Disabled base tools: {disabled_base}")

    # 6. 获取子 Agent 的 MCP 工具 schema
    mcp_tools_schema = get_subagent_mcp_tools_schema(agent_name)
    if mcp_tools_schema:
        tools_schema = tools_schema + mcp_tools_schema
        logger.info(f"[SubAgent] {agent_name}: {len(tools_schema)} tools ({len(mcp_tools_schema)} MCP)")
    else:
        logger.warning(f"[SubAgent] {agent_name}: {len(tools_schema)} tools (0 MCP - WARNING: No MCP tools loaded!)")

    # 列出关键工具（调试）
    tool_names = [t.get("function", {}).get("name", "") for t in tools_schema]
    logger.debug(f"[SubAgent] {agent_name}: Tools = {tool_names}")

    # no_tools 模式：清空所有工具，LLM 只能直接回复文本
    if no_tools:
        tools_schema = []
        logger.info(f"[SubAgent] {agent_name}: no_tools=True, all tools disabled")

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
    )

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

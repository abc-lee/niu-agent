"""
SubAgent Module

子 Agent 调用机制。
"""

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from .subagent_registry import SubagentRegistry

DEFAULT_CONTEXT_WINDOW_SIZE = 200000
MIN_CONTEXT_WINDOW_SIZE = 32000    # 32K 最小合理值
MAX_CONTEXT_WINDOW_SIZE = 2000000  # 2M 上限

# 基础工具全集（agent/generic/assets/tools_schema.json 定义的 6 个系统工具）
# 子 Agent 白名单制：缺省没有任何基础工具，frontmatter allowBaseTools 声明哪个有哪个
_BASE_TOOL_NAMES = {"bash", "code_run", "read", "write", "edit", "grep"}


def _filter_base_tools(agent_config: dict, tools_schema: list) -> tuple:
    """根据 agent_config 的 allowBaseTools 白名单过滤基础工具。

    白名单逻辑（与 MCP 工具 mcpServers/mcpToolFilter 白名单语义一致）：
    - 缺省（未配置 allowBaseTools）：子 Agent 没有任何基础工具
    - allowBaseTools 声明的工具保留，其余全部移除
    - allowBaseTools 中出现不存在的工具名（笔误）打 warning 日志

    Args:
        agent_config: 子 Agent 配置字典（frontmatter 解析结果）
        tools_schema: 待过滤的工具 schema 列表（6 个基础工具）

    Returns:
        (filtered_tools, allowed_base) 元组：
        - filtered_tools: 过滤后的工具 schema 列表
        - allowed_base: 子 Agent 配置的 allowBaseTools 列表
    """
    allowed_base = agent_config.get("allowBaseTools", []) or []

    # 笔误防护：白名单中出现不存在的工具名打 warning
    unknown = [n for n in allowed_base if n not in _BASE_TOOL_NAMES]
    if unknown:
        logger.warning(
            f"[SubAgent] allowBaseTools contains unknown tool names (typo?): {unknown}. "
            f"Valid base tools: {sorted(_BASE_TOOL_NAMES)}"
        )

    allowed_set = set(allowed_base) & _BASE_TOOL_NAMES
    filtered = [
        t for t in tools_schema
        if t.get("function", {}).get("name", "") in allowed_set
    ]

    return filtered, allowed_base


# 子 Agent 职责边界段模板（自动注入到正文未含"直接退出"语义的子 Agent）
_BOUNDARY_SECTION_TEMPLATE = """## 职责边界

你的职责范围由上方系统提示词界定的功能描述决定。
不要猜测含义，无法完全确认属于自己的职责范围的，就要直接退出，回复主 Agent。"""


_SUBAGENT_ASK_GUIDE_TEMPLATE = """<!-- NIU_SUBAGENT_GUIDE_v3 -->
## 通讯语法
- `@niu-agent 问题内容` — 向主 Agent 提问，阻塞等待回答（5 分钟超时）
- `@user 问题内容` — 向用户提问，阻塞等待回答（10 分钟超时）
- `@end` — 任务完成，退出
"""

_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v3 -->"


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
    """Locate user-config.json under ~/.niu/config/."""
    from niu_api.config import CONFIG_PATH
    return Path(CONFIG_PATH)


def _read_context_window_tokens() -> int:
    """Read context window size from config/user-config.json."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, encoding="utf-8") as f:
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
        key: Field name in context section (e.g. 'warningThreshold')
        default: Default value if key not found or invalid
    """
    try:
        config_path = _get_user_config_path()
        with open(config_path, encoding="utf-8") as f:
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


DEFAULT_PROTECT_RECENT_COUNT = 10


def _read_protect_recent_count() -> int:
    """Read protectRecentCount from config/user-config.json. Default 10."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, encoding="utf-8") as f:
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
        with open(config_path, encoding="utf-8") as f:
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


def _maybe_push_subagent_instruction(handler_or_unique_name, content) -> bool:
    """推送主 Agent 指令/回答到子 Agent 前端 tab。

    在三个推送点调用：
    1. _run_agent_loop 开头（初始指令，传 handler）
    2. route_to_subagent sender='主Agent'（异步回答，传 unique_name 字符串）
    3. call_subagent 续答路径（同步回答，传 unique_name 字符串）

    兼容两种参数：handler 对象（取 _subagent_unique_name 属性）或 unique_name 字符串。

    Args:
        handler_or_unique_name: NiuHandler 实例（有 _subagent_unique_name 属性）或 unique_name 字符串。
        content: 要推送的文本（主 Agent 指令或回答）。

    Returns:
        True 表示已推送，False 表示跳过（内容为空或无 unique_name）。
    """
    if not content:
        return False
    # 兼容 handler 对象和 unique_name 字符串
    if isinstance(handler_or_unique_name, str):
        unique_name = handler_or_unique_name
    else:
        unique_name = getattr(handler_or_unique_name, '_subagent_unique_name', None)
    if not unique_name:
        return False
    try:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, 'instruction', {'content': content})
    except ImportError:
        pass  # niu_api 未启动，静默降级
    except Exception:
        pass  # 推送失败不影响子 Agent 循环
    return True


def _run_agent_loop(
    client,
    system_prompt: str = "",  # 向后兼容（system_message 非 None 时优先）
    system_message: dict | None = None,  # 已组装好的 system message（首轮即带 cache_control）
    user_input: str = "",
    handler=None,
    tools_schema: list = None,
    max_turns: int | None = None,  # None = 无上限（子 Agent 长程任务跑到底）
    initial_user_content: str | None = None,
    context_window_tokens: int = 0,
    context_fifo_threshold: int = 0,
    context_target_threshold: int = 0,
    history: list | None = None,
    supplement_queue: Any | None = None,  # 子 Agent 独立 supplement queue
    memory_context: Any | None = None,  # 阶段二新增：异步子 Agent 进度数据
    resumed_messages: list | None = None,  # 阶段四新增：断点续传消息列表
    stop_predicate: Any | None = None,  # 停止穿透：子 Agent 循环层停止谓词（LLM 层 stop_check 同源）
) -> tuple[str, Any, str]:
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
        max_turns: 最大轮次；None = 无上限（子 Agent 长程任务跑到底）
        initial_user_content: 初始用户内容（如果不提供则使用 user_input）
        context_window_tokens: 上下文窗口 token 数（0 表示不检查）

    Returns:
        (result, return_value, last_reply) 三元组
        - result: 所有轮次 reply 累加（保留向后兼容）
        - return_value: agent_runner_loop 的返回值 dict
        - last_reply: 最后一次 reply 的内容（完成通知用）
    """
    from .generic.agent_loop import StreamEvent, agent_runner_loop

    if initial_user_content is None:
        initial_user_content = user_input

    # 推送主 Agent 初始指令到子 Agent tab（早于 reply/thinking/tool_status）
    _maybe_push_subagent_instruction(handler, initial_user_content)

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
        resumed_messages=resumed_messages,  # 阶段四新增：透传给 agent_runner_loop
        stop_predicate=stop_predicate,  # 停止穿透：子 Agent 循环层停止谓词（LLM 层 stop_check 同源）
    )
    result = ""
    last_reply = ""  # 只记录最后一次 reply 的内容（完成通知用）
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
                    last_reply = chunk.content  # 只保留最后一次 reply
                    # 子 Agent 回复文本推送到 SubagentEventBus（前端 tab 展示）
                    unique_name = getattr(handler, '_subagent_unique_name', None)
                    if unique_name:
                        try:
                            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
                            notify_subagent_event_sync(unique_name, 'reply', {'content': chunk.content})
                        except ImportError:
                            pass
                elif chunk.type in ('persist', 'system', 'tool_marker'):
                    unique_name = getattr(handler, '_subagent_unique_name', None)
                    if unique_name:
                        try:
                            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
                            notify_subagent_event_sync(unique_name, chunk.type, {'content': chunk.content})
                        except ImportError:
                            pass
            else:
                logger.warning(f"[SubAgent] Non-string chunk from agent_runner_loop: {type(chunk).__name__}")
        except StopIteration as e:
            return_value = e.value
            break

    return result, return_value, last_reply


def _strip_at_prefix(answer: str, unique_name: str) -> str:
    """剥除 answer 的 '@unique_name ' 前缀。找不到前缀原样使用，记 warning。

    阶段四：第三分支（回复路径）用，剥除 @子名 前缀把纯回答内容传给子 Agent。
    """
    pattern = rf"^@{re.escape(unique_name)}\s+"
    match = re.match(pattern, answer)
    if match:
        return answer[match.end():]
    logger.warning(f"[StripAtPrefix] answer 不含 @{unique_name} 前缀，原样使用: {answer[:100]}")
    return answer


def _maybe_suspend_session(unique_name, return_value, handler, client, tools_schema, system_message):
    """检测同步 @niu-agent 挂起信号，存挂起状态到 registry。

    必须在 try 块内、finally 之前调用（异常安全）。
    阶段四：同步子 Agent 跑出 INTERCEPTED_SYNC 时，把 session 状态存到 registry，
    让主 Agent 第二次 call_subagent(answer=...) 时能从 registry 拿回继续跑。
    """
    from .subagent_registry import SubagentRegistry
    if not (return_value and isinstance(return_value, dict)):
        return
    result_flag = return_value.get("result", "")
    if result_flag != "INTERCEPTED_SYNC":
        return
    if not getattr(handler, "_is_sync_subagent", False):
        return
    try:
        instance = SubagentRegistry.get(unique_name)
        if not instance:
            return
        msgs = return_value.get("messages", [])
        if not msgs or not isinstance(msgs[0], dict) or msgs[0].get("role") != "system":
            logger.error("[MaybeSuspend] return_value messages 异常（空或首条非 system），不挂起")
            return
        instance.state = "waiting_for_answer"
        instance.suspended_messages = msgs
        instance.suspended_handler = handler
        instance.suspended_client = client
        instance.suspended_tools_schema = tools_schema
        instance.suspended_system_message = system_message
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            notify_subagent_event_sync(unique_name, 'subagent_suspended', {})
        except ImportError:
            pass
    except Exception as e:
        logger.error(f"[MaybeSuspend] helper 异常，强制设 state=waiting_for_answer: {e}")
        try:
            instance = SubagentRegistry.get(unique_name)
            if instance:
                instance.state = "waiting_for_answer"
                if instance.suspended_messages is None:
                    msgs = return_value.get("messages", [])
                    if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                        instance.suspended_messages = msgs
                if instance.suspended_handler is None:
                    instance.suspended_handler = handler
                if instance.suspended_client is None:
                    instance.suspended_client = client
                if instance.suspended_tools_schema is None:
                    instance.suspended_tools_schema = tools_schema
                if instance.suspended_system_message is None:
                    instance.suspended_system_message = system_message
        except Exception as fallback_err:
            logger.error(f"[MaybeSuspend] fallback 也失败: {fallback_err}")
            raise RuntimeError(f"_maybe_suspend_session fallback 失败: {fallback_err}") from fallback_err


def _extract_result_from_return_value(return_value: Any) -> str | None:
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
        control_flow_results = {
            "CONTEXT_OVERFLOW", "EXITED", "MAX_TURNS_EXCEEDED", "CURRENT_TASK_DONE", "TERMINATED_BY_SUPPLEMENT",
            "STOPPED",           # 阶段四补：子 Agent 收到 /stop 终止
            "INTERCEPTED_SYNC",  # 阶段四新增：同步 @niu-agent 挂起
            "LLM_ERROR",         # LLM 流式错误（重试耗尽或 fatal），不应被序列化为结果
        }
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


def _resolve_agent_md_path(agent_name: str) -> str | None:
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


def get_subagent_config(agent_name: str) -> dict[str, Any]:
    """
    获取子 Agent 配置

    Args:
        agent_name: 子 Agent 名称（如 file-processor、photo-organizer）

    Returns:
        配置字典，包含 mcpServers 等字段。MD 文件不存在时返回空 dict。
    """
    prompt_path = _resolve_agent_md_path(agent_name)

    if prompt_path and os.path.exists(prompt_path):
        with open(prompt_path, encoding="utf-8") as f:
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
        with open(prompt_path, encoding="utf-8") as f:
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

    # 3.5 为 dream-evolver 预注入当前脑区列表（注入 dynamic_system，因为脑区列表会变化，不适合放在 cache 前缀的 static_system 中）
    _brain_region_section = ""
    if agent_name == "dream-evolver":
        try:
            from niu_api.internal.lightrag_manager import get_brain_regions
            brain_regions = get_brain_regions()
            if brain_regions:
                region_list = "、".join(brain_regions)
                _brain_region_section = f"\n\n## 当前脑区列表（预注入，无需搜索）\n\n{region_list}\n\n创建实体时直接参考以上脑区列表选择归属，不要调用 lightrag_search_entities 查询脑区。"
        except Exception as e:
            logger.debug(f'[SubAgent] Failed to get brain regions for {agent_name}: {e}')

    # 4. 强制注入 @niu-agent/@end 守则
    # context-manager 例外：它原设计是直接输出 keep=/update=/cursor= 让程序写数据库，
    # 不走 @niu-agent/@end 交互通道。注入守则会污染它的输出格式，导致压缩失败。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    if agent_name != "context-manager" and _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE

    # 5. 动态段：Current Time
    try:
        from datetime import datetime
        dynamic_system = f"\n\nCurrent Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + _brain_region_section
    except Exception:
        dynamic_system = _brain_region_section

    return static_system, dynamic_system


def get_subagent_mcp_tools_schema(agent_name: str) -> list[dict]:
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

    注入内容：
    - user 字段：全部内容（姓名/称呼/职业/工作单位/技能等），值以"请询问"开头则跳过
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

    # 用户信息：user 区全部内容（姓名/称呼/职业/工作单位/技能等）
    user = memory.get("user", {})
    if isinstance(user, dict) and user:
        user_lines = []
        field_labels = {
            "name": "姓名",
            "nickname": "称呼",
            "occupation": "职业",
            "organization": "工作单位",
            "skills": "技能",
        }
        for key, label in field_labels.items():
            value = user.get(key)
            if not value or (isinstance(value, str) and value.startswith("请询问")):
                continue
            if isinstance(value, list):
                value = "、".join(str(v) for v in value)
            user_lines.append(f"- {label}：{value}")
        # 兜底：user 区有其他未列字段也注入
        for key, value in user.items():
            if key in field_labels:
                continue
            if not value or (isinstance(value, str) and value.startswith("请询问")):
                continue
            if isinstance(value, list):
                value = "、".join(str(v) for v in value)
            user_lines.append(f"- {key}：{value}")
        if user_lines:
            sections.append("## 用户信息\n\n" + "\n".join(user_lines))

    # 用户偏好（仅 type="memory"）
    permanent = memory.get("permanent", [])
    memory_items = [item for item in permanent if item.get("type") == "memory" and item.get("content")]
    if memory_items:
        pref_lines = [f"{i}. {item['content']}" for i, item in enumerate(memory_items, 1)]
        sections.append("## 用户偏好\n\n" + "\n".join(pref_lines))

    return "\n\n".join(sections)


def _build_subagent_tools_schema(
    agent_name: str,
    agent_config: dict | None = None,
    memory_context: Any | None = None,
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
    # 白名单过滤：缺省零基础工具，allowBaseTools 声明哪个有哪个
    tools_schema, _ = _filter_base_tools(agent_config, tools_schema)
    allowed_names = sorted(t.get("function", {}).get("name", "") for t in tools_schema)
    if allowed_names:
        logger.info(f"[SubAgent] {agent_name}: Allowed base tools: {allowed_names}")
    else:
        logger.info(f"[SubAgent] {agent_name}: No base tools (allowBaseTools empty or not configured)")

    # MCP 工具
    mcp_tools_schema = get_subagent_mcp_tools_schema(agent_name)
    if mcp_tools_schema:
        tools_schema = tools_schema + mcp_tools_schema
        logger.info(f"[SubAgent] {agent_name}: {len(tools_schema)} tools ({len(mcp_tools_schema)} MCP)")
    else:
        logger.warning(f"[SubAgent] {agent_name}: {len(tools_schema)} tools (0 MCP - WARNING: No MCP tools loaded!)")

    # 列出关键工具（调试）
    tool_names = [t.get("function", {}).get("name", "") for t in tools_schema]
    logger.debug(f"[SubAgent] {agent_name}: Tools = {tool_names}")

    return tools_schema


def call_subagent(
    agent_name: str,
    task: str,
    llm_config: dict[str, Any],
    mcp_client=None,
    history: list | None = None,
    context_fifo_threshold: int = -1,
    no_tools: bool = False,
    supplement_queue: Any | None = None,
    memory_context: Any | None = None,  # 阶段二新增：异步子 Agent 进度数据
    unique_name: str | None = None,  # 阶段二新增：异步路径透传，跳过内部 register
    answer: str | None = None,  # 阶段四新增：回复路径（第三分支）用
    answer_unique_name: str | None = None,  # 阶段四新增：回复路径锁定挂起 session
    bypass_at_prefix: bool = False,  # True=绕过@前缀拦截层（仅一轮出方案的子Agent用，如context-manager模式二/三）
    program_triggered: bool = False,  # 程序触发子 Agent（如 entity-extractor）跳过超长写文件，保留游标标记
    max_turns: int | None = None,  # 子 Agent 轮数上限；None = 无上限（长程任务跑到底）
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
        max_turns: 子 Agent 轮数上限；None = 无上限（长程任务跑到底）。显式传小值仍触发 MAX_TURNS_EXCEEDED

    Returns:
        子 Agent 执行结果
    """
    from .handler import NiuHandler

    # 顶部校验：在 get_subagent_config 之前
    if not task and not answer:
        return "[错误] chat-with-xxx 必须传 task（新任务）或 answer + unique_name（回复子 Agent 问题）"

    # 1. 获取子 Agent 提示词 + temperature
    agent_config = get_subagent_config(agent_name)
    if agent_config.get("temperature") is not None:
        llm_config = {**llm_config, "temperature": agent_config["temperature"]}

    # 2. 构建静态/动态段（cache 友好）
    try:
        static_system, dynamic_system = build_subagent_system_segments(agent_name)
    except Exception as e:
        logger.warning(f'[SubAgent] build_subagent_system_segments failed for {agent_name}, falling back to bare prompt: {e}')
        try:
            static_system = get_subagent_prompt(agent_name)
        except Exception:
            static_system = f'You are {agent_name} sub-agent. Complete the task efficiently.'
        dynamic_system = ""

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

    # 停止隔离：按来源与同步/异步绑定 stop 谓词（LLM 层 stop_check 与循环层 stop_predicate 同源）
    # - 同步 user 子 Agent（含同步恢复）：全局 stop（单击停主 Agent 连带停同步子 Agent） or terminate（双击）
    # - 异步 user 子 Agent：仅 terminate（单击不连累异步子 Agent）
    # - program / scheduler 子 Agent：仅 terminate（用户双击不置位 → 永不被打断）
    import threading as _threading
    from agent.runner import get_runner as _get_runner
    from agent.runner import is_stop_requested as _is_stop_global
    from .subagent_registry import SubagentRegistry  # 顶部绑定需读异步实例 source 快照（既有 import 在其后，先局部导入避免 NameError）
    terminate_event = _threading.Event()
    if program_triggered:
        _source = "program"
    elif unique_name is not None and answer is None:
        # 异步路径：source 从派发时快照读（_dispatch_async_subagent 已在派发线程写入实例，
        # 避免 to_thread 线程读 runner._request_source 已恢复默认的竞态）
        _async_inst = SubagentRegistry.get(unique_name)
        _source = getattr(_async_inst, "source", "user") if _async_inst is not None else "user"
    else:
        # 同步路径（含恢复）：从 runner 当前请求来源读
        _source = getattr(_get_runner(), "_request_source", "user")
    _is_async = (unique_name is not None and answer is None)
    if _is_async or _source != "user":
        _stop_fn = lambda: terminate_event.is_set()
    else:
        _stop_fn = lambda: _is_stop_global() or terminate_event.is_set()
    client.backend.stop_check = _stop_fn

    # 4. 创建 handler（禁用记忆检索，子 Agent 不需要）
    handler = NiuHandler(mcp_client=mcp_client)
    handler._disable_memory_recall = True
    # 重要约定：子 Agent 必须标记 _is_subagent = True
    # 否则子 Agent 的工具调用会触发 brain region reinforcement（应只由主 Agent 触发）
    # 新增子 Agent 时必须遵守此约定
    handler._is_subagent = True
    # @前缀拦截层绕过开关：仅一轮出方案的子 Agent（context-manager 模式二/三）由调用方
    # 显式传 bypass_at_prefix=True 开启；模式一（多轮工具）保持默认 False，走标准 @end/FORMAT_ERROR 结束判断
    handler._bypass_at_prefix = bypass_at_prefix
    handler._program_triggered = program_triggered

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

    # FIFO 裁剪目标 token 量（写死 50% 窗口——targetThreshold 配置已删除，用户拍板：80% 触发 → 压到 50%）
    context_target_threshold_val = int(context_window_tokens * 0.50) if context_window_tokens > 0 else 0

    # === 创建 supplement queue + 注册到 SubagentRegistry ===
    from .subagent_registry import SubagentRegistry
    from .subagent_supplement import SubagentSupplementQueue

    # 初始化 last_reply（三条路径统一用，最终返回优先用它）
    last_reply = ""
    if answer is not None and answer_unique_name is not None:
        # 阶段四第三分支：回复路径——从 registry 拿回挂起 session 继续跑
        instance = SubagentRegistry.get(answer_unique_name)
        if instance is None or getattr(instance, "state", None) != "waiting_for_answer":
            return f"[错误] 找不到挂起的子 Agent session（unique_name={answer_unique_name}），可能已被终止"
        if instance.agent_type != agent_name:
            return f"[错误] unique_name={answer_unique_name} 不属于子 Agent {agent_name}（实际属于 {instance.agent_type}），请检查 unique_name 是否传错"

        reply_text = _strip_at_prefix(answer, answer_unique_name)

        suspended_messages = instance.suspended_messages
        suspended_messages.append({"role": "user", "content": f"[主 Agent 回答] {reply_text}"})

        instance.state = "running"
        # 续答期间用 suspended_handler 跑，同步 handler 引用供使用率读取
        instance.handler = instance.suspended_handler
        # 注释：不预检查 supplement_queue 是否已有 /stop，依赖 agent_runner_loop 内部 drain 检测

        try:
            instance.suspended_handler._subagent_unique_name = answer_unique_name
            # 推送主 Agent 回答到子 Agent tab（同步续答路径）
            _maybe_push_subagent_instruction(answer_unique_name, reply_text)
            # 恢复路径：stop 谓词必须闭包实例的 terminate_event（首次注册的 E1，挂起期间不被替换）
            _instance_ev = getattr(instance, "terminate_event", None)
            _inst_terminate = (lambda: _instance_ev.is_set()) if _instance_ev else (lambda: False)
            if getattr(instance, "source", "user") != "user":
                # program/scheduler 子 Agent 恢复：仅 terminate（单击全局 stop 不打断）
                _stop_fn = _inst_terminate
            else:
                # user 子 Agent 恢复（同步语义）：global or terminate
                _stop_fn = lambda: _is_stop_global() or _inst_terminate()
            result_text, return_value, last_reply = _run_agent_loop(
                client=instance.suspended_client,
                system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
                system_message=instance.suspended_system_message,
                user_input="",
                initial_user_content=None,
                handler=instance.suspended_handler,
                tools_schema=instance.suspended_tools_schema,
                max_turns=max_turns,  # resume 路径同源透传（None = 无上限）
                memory_context=None,
                resumed_messages=suspended_messages,
                supplement_queue=instance.supplement_queue,
                stop_predicate=_stop_fn,  # 停止穿透：恢复路径重建谓词（闭包实例 E1 + 按实例 source 区分）
            )
            _maybe_suspend_session(
                unique_name=answer_unique_name,
                return_value=return_value,
                handler=instance.suspended_handler,
                client=instance.suspended_client,
                tools_schema=instance.suspended_tools_schema,
                system_message=instance.suspended_system_message,
            )
        finally:
            final_instance = SubagentRegistry.get(answer_unique_name)
            final_state = getattr(final_instance, "state", None) if final_instance else None
            if final_state != "waiting_for_answer":
                SubagentRegistry.unregister(answer_unique_name)
        # 注意：正常路径跑完后控制流落到 L825+ 后处理（截断/overflow/extract）
        # 错误路径（[错误] ...）直接 return，不落后处理
    elif unique_name is not None:
        # 异步路径：调用方已注册（_dispatch_async_subagent），跳过内部 register
        # 只设置 handler._subagent_unique_name（handler.dispatch 的 ask_main_agent 分支用）
        handler._subagent_unique_name = unique_name
        # 回填 handler 引用到 registry（/api/stats?agent= 读取该子 Agent 真实使用率）
        _instance = SubagentRegistry.get(unique_name)
        if _instance is not None:
            _instance.handler = handler
            _instance.terminate_event = terminate_event
            _instance.source = _source
        # 阶段四：异步路径不是同步子 Agent
        handler._is_sync_subagent = False
        # supplement_queue 也由调用方传入，不重新创建
        try:
            result_text, return_value, last_reply = _run_agent_loop(
                client=client,
                system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=max_turns,  # None = 无上限
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,  # 调用方传入
                memory_context=memory_context,  # 阶段二新增：透传给 _run_agent_loop
                stop_predicate=_stop_fn,  # 停止穿透：异步路径用顶部绑定（仅 terminate）
            )
        finally:
            # 异步路径不在这里 unregister（_run_subagent_async 的 finally 负责）
            pass
        # 异步路径：把 last_reply 存到 registry instance 供 _run_subagent_async 使用
        instance = SubagentRegistry.get(unique_name)
        if instance is not None:
            instance.last_reply = last_reply
    else:
        # 同步路径：用 agent_name 作 unique_name（避免 LLM 记随机 hex 后缀）
        if supplement_queue is None:
            supplement_queue = SubagentSupplementQueue(unique_name="")
        try:
            unique_name = SubagentRegistry.register(
                agent_name, supplement_queue, force_unique_name=agent_name, handler=handler,
            )
        except ValueError as e:
            return f"[错误] {e}。请先用 chat-with-{agent_name}(answer=...) 回复当前挂起的子 Agent，或等它结束。"
        supplement_queue.unique_name = unique_name  # 回填唯一名（= agent_name）
        _sync_inst = SubagentRegistry.get(unique_name)
        if _sync_inst is not None:
            _sync_inst.terminate_event = terminate_event
            _sync_inst.source = _source
        handler._subagent_unique_name = unique_name
        handler._is_sync_subagent = True
        try:
            result_text, return_value, last_reply = _run_agent_loop(
                client=client,
                system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=max_turns,  # None = 无上限
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,  # 新增：传给 _run_agent_loop
                memory_context=memory_context,  # 阶段二新增：透传给 _run_agent_loop
                stop_predicate=_stop_fn,  # 停止穿透：同步路径用顶部绑定（user = global or terminate / 非 user = 仅 terminate）
            )
            # §5.5 后处理：必须在 try 块内、finally 之前执行（异常时跳过，直接进 finally）
            _maybe_suspend_session(
                unique_name=unique_name,
                return_value=return_value,
                handler=handler,
                client=client,
                tools_schema=tools_schema,
                system_message=system_message,
            )
        finally:
            # 条件化 unregister：state="waiting_for_answer" 时跳过（挂起 session 留待第二次 call_subagent 接续）
            instance = SubagentRegistry.get(unique_name)
            state = getattr(instance, "state", None) if instance else None
            if state != "waiting_for_answer":
                SubagentRegistry.unregister(unique_name)

    # LLM_ERROR：流式错误（重试耗尽或 fatal），返回 SUBAGENT_ERROR 前缀让调用方降级
    if return_value and isinstance(return_value, dict) and return_value.get("result") == "LLM_ERROR":
        error_msg = return_value.get("error_msg", "未知错误")
        logger.warning(f"[SubAgent] {agent_name}: LLM error: {error_msg}")
        return f"SUBAGENT_ERROR:{error_msg}"

    # 检测输出截断（finish_reason == "length"）
    # LLM 输出被截断时无法产出合法 keep/update 结构，返回字符串信号让降级循环识别
    if return_value and isinstance(return_value, dict):
        if return_value.get("finish_reason") == "length":
            logger.warning(f"[SubAgent] {agent_name}: Output truncated (finish_reason=length)")
            truncated_content = last_reply or result_text or "[输出因超过最大长度被自动截断，无有效内容]"
            return f"COMPACT_TRUNCATED:{truncated_content}"

    # CONTEXT_OVERFLOW：返回结构化进度报告
    if return_value and isinstance(return_value, dict) and return_value.get("result") == "CONTEXT_OVERFLOW":
        data = return_value.get("data", {})
        overflow_report = {
            "overflow": True,
            "agent": agent_name,
            "turns_completed": data.get("turns_completed", 0),
            "tokens_used": data.get("tokens_used", 0),
            "tokens_limit": data.get("tokens_limit", 0),
            "partial_result": result_text or "",
        }
        logger.warning(f"[SubAgent] {agent_name}: Context overflow at {data.get('tokens_used', 0)} tokens")
        return json.dumps(overflow_report, ensure_ascii=False)

    # 优先从 return 值提取结构化结果
    extracted = _extract_result_from_return_value(return_value)
    if extracted is not None:
        return extracted

    # 优先返回 last_reply（最终报告），fallback 到 result_text（全过程累加）
    if last_reply:
        return last_reply
    return result_text


def call_subagent_with_auto_answer(agent_name, task, **kwargs):
    """程序触发子 Agent 专用：自动回复 @niu-agent，遇到 @end 或正常文本才返回。

    与主 Agent 调用 call_subagent 不同：程序触发（force 压缩 / 手动 tidy API）
    时没有主 Agent 在工具循环里等子 Agent 回答。子 Agent 输出 [unique_name] question
    格式时，由本 helper 自动回复固定文案，让子 Agent 自行决策继续或 @end 结束。

    Args:
        agent_name: 子 Agent 名（如 file-processor）
        task: 任务描述
        **kwargs: 透传给 call_subagent（如 llm_config、mcp_client、no_tools 等）

    Returns:
        子 Agent 最终非 @niu-agent 输出（正常结果 / @end 汇报）
    """
    auto_answer = "无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作"

    # 程序触发子 Agent：超长报告不写文件，保留 processed_up_to 游标标记
    kwargs.setdefault('program_triggered', True)

    # 【subagent_started 补发】程序触发路径（nap/sleep/force 整理）此前不发 subagent_started 事件，
    # 前端收不到启动通知 → 不建 tab、不连 SSE，用户在界面看不到系统子 Agent 的工作过程。
    # 仅首次调用（新任务）推送；恢复回答路径（while 循环内直调 call_subagent 带 answer）不重入本函数，
    # 无需重复推（守卫防御性保留：若未来有人带 answer 调用本函数，不重复建 tab）。
    # 与 handler.py 同步路径发射点同款：pre_register 先建 ring buffer（防前端连 SSE 404 竞态），
    # 再经主事件循环 call_soon_threadsafe 推 subagent_started（本函数在 to_thread 后台线程执行，线程安全）。
    # is_sync=False：程序触发不阻塞主对话（sleep/nap 后台整理），tab 显示"正在启动..."占位文案。
    # 注：异常清理在下方 try/except（无条件 close，同 handler.py 问题2c/2e 模式）。
    if kwargs.get('answer') is None:
        try:
            from niu_api.internal.subagent_event_bus import pre_register
            pre_register(agent_name)
        except ImportError:
            pass
        try:
            from niu_api.chat import _main_loop, _sync_broadcast
            if _main_loop and not _main_loop.is_closed():
                event = {
                    'type': 'subagent_started',
                    'unique_name': agent_name,
                    'agent_name': agent_name,
                    'is_sync': False,
                }
                _main_loop.call_soon_threadsafe(_sync_broadcast, event)
        except ImportError:
            pass

    try:
        result = call_subagent(agent_name=agent_name, task=task, **kwargs)
    except Exception:
        # 【异常清理】仅首次调用（我们建了 ring buffer）时，若 call_subagent 在 register 之前抛异常
        # （llm_config 非法、agent 配置缺失、schema 构建失败等），pre_register 的 ring buffer 可能已存在
        # 但无人 close → 前端已建 tab 连 SSE 后无限 keepalive、永远"正在启动..."。
        # 与 handler.py【问题2c/2e】同款清理：close 幂等（_closed set 防双关），无条件 close：
        # ① register 成功但运行异常 → register 的 finally unregister→close 已置 _closed → 此处被拦截不双关；
        # ② register 前异常 → 直接清理（无 buffer 时 notify/cleanup 无害）；
        # ③ 不做 has_subagent 检查——避免与异步调度的 _do_pre_register 的窄竞态（检查时 buffer 未建、
        #    跳过清理后主 loop 仍建 buffer + 广播 → tab 卡死，恰是修复想防的故障）。
        # 恢复路径（answer 非 None）异常由 call_subagent answer 分支 finally unregister→close 负责。
        if kwargs.get('answer') is None:
            try:
                from niu_api.internal.subagent_event_bus import close
                close(agent_name)
            except Exception:
                pass
        raise

    # 【值错误路径清理】register 失败不抛异常——call_subagent 内部捕获 ValueError 返回 '[错误]...'
    # 字符串（如同名实例已存在：前一轮挂起残留/并发同 agent 触发，subagent.py L1016-1017）。
    # 此时 pre_register 的 ring buffer + 前端 tab 已建，无人 close → tab 永久"正在启动..."。
    # 首次调用结果以 '[错误]' 开头（register 失败专属前缀）→ close 清理，tab 立即 completed 关闭。
    if kwargs.get('answer') is None and isinstance(result, str) and result.startswith('[错误]'):
        try:
            from niu_api.internal.subagent_event_bus import close
            # 【归属守卫】register 失败（ValueError）意味着同名实例已存在（活跃或挂起）——
            # 其 tab/ring buffer 归活跃实例所有，失败方 close 会误关其 tab、断开 SSE、清空其事件。
            # 仅当实例已不存在（get() 返回 None，如极端时序实例已 unregister）时才 close，
            # 清理可能残留的 buffer。SubagentRegistry 有锁，无 has_subagent 式异步竞态。
            if SubagentRegistry.get(agent_name) is None:
                close(agent_name)
        except Exception:
            pass
    while True:
        unique_name = _extract_unique_name(result, agent_name)
        if unique_name is None:
            return result  # 非 @niu-agent 问题，正常返回
        result = call_subagent(
            agent_name=agent_name,
            task="",
            answer=auto_answer,
            answer_unique_name=unique_name,
            **kwargs,
        )


def _extract_unique_name(result, agent_name):
    """从 '[unique_name] ...' 提取 unique_name，不匹配返回 None。

    支持两种格式（向后兼容）：
    - 同步路径：[agent_name] 问题（如 [browser-operator] 第一个问题）
    - 异步路径：[agent_name-4位hex] 问题（如 [file-processor-a1b2] 第一个问题）

    严格匹配避免误判 `[已完成]` 等正常文本。
    """
    # 优先匹配带 hex 后缀（异步路径）
    pattern_with_hex = rf"^\[({re.escape(agent_name)}-[0-9a-f]{{4}})\] "
    m = re.match(pattern_with_hex, result)
    if m:
        return m.group(1)
    # 再匹配纯 agent_name（同步路径）
    pattern_plain = rf"^\[({re.escape(agent_name)})\] "
    m = re.match(pattern_plain, result)
    return m.group(1) if m else None


# ==================== 阶段二：ask_main_agent 工具 ====================


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
    from .ask_main_agent import TERMINATED_SIGNAL, get_pending_ask_registry
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
    # 超长检查已在 _intercept_at_prefix_content 中处理（FORMAT_ERROR 退回重试）
    # 不截断、不剥行首 @（[子名] 前缀已标识提问）
    sanitized_question = question if question else ""
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

def _ask_user_impl(question: str, unique_name: str) -> str | None:
    """子 Agent 向用户提问，阻塞等待用户回答。

    1. 推送 question 事件到 SubagentEventBus（前端 tab 高亮显示问题）
    2. 设 state=waiting_for_user
    3. 注册 AskUserFuture，阻塞等待
    4. 设置 state=running（仅在子 Agent 仍存活时）
    5. 返回用户回答
    """
    from agent.ask_user import get_user_ask_registry, TERMINATED_SIGNAL
    from agent.subagent_registry import SubagentRegistry

    instance = SubagentRegistry.get(unique_name)
    if instance is None:
        return '[ask_user 错误] 子 Agent 已不在注册表'

    # 检查 _ask_user_terminated 标志（/stop 在 register 之前到达）
    if getattr(instance, '_ask_user_terminated', False):
        return TERMINATED_SIGNAL

    # 推送问题到前端
    try:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, 'question', {'content': question[:2000]})
    except ImportError:
        logger.warning('[ask_user] SubagentEventBus not available, question event not pushed to frontend')

    # 设 state
    instance.state = 'waiting_for_user'

    # 注册 future 并阻塞
    registry = get_user_ask_registry()
    future = registry.register(unique_name)
    try:
        answer = future.wait()  # 用 AskUserFuture 默认 _ASK_TIMEOUT=600
        if answer == TERMINATED_SIGNAL:
            return TERMINATED_SIGNAL
        return answer
    finally:
        # 仅在子 Agent 仍存活时恢复 state
        if SubagentRegistry.get(unique_name) is instance:
            instance.state = 'running'
        registry.unregister(unique_name)


def _ask_main_agent_impl_sync(
    question: str,
    unique_name: str,
    handler,
    messages: list,
    content: str,
) -> str:
    """同步路径：包装 question 为 [unique_name] question，append assistant content 到 messages。

    与异步 _ask_main_agent_impl（subagent.py:782）的包装逻辑一致，但：
    - 不阻塞等主 Agent 回答（同步路径靠工具返回值通道）
    - 不推 MainAgentRequestQueue（同步路径不走 db_monitor）
    - append assistant content 保留对话历史，不 append user（user 由第二次 call_subagent 注入）

    Args:
        question: 子 Agent 要问的问题
        unique_name: 子 Agent 唯一名
        handler: 子 Agent handler（保留参数，与异步签名风格一致；当前实现未使用）
        messages: 子 Agent messages 列表（in-place 修改，append assistant content）
        content: 原始 LLM 输出文本（含 @niu-agent 前缀）

    Returns:
        包装后的文本 "[unique_name] sanitized_question"
    """
    messages.append({"role": "assistant", "content": content})

    # 超长检查已在 _intercept_at_prefix_content 中处理（FORMAT_ERROR 退回重试）
    # 不截断、不剥行首 @（[子名] 前缀已标识提问）
    sanitized = question if question else ""
    wrapped = f"[{unique_name}] {sanitized}"
    return wrapped


# ==================== 阶段二：异步子 Agent 派发与运行 ====================


def _dispatch_async_subagent(
    agent_name: str,
    task: str,
    llm_config: dict[str, Any],
    mcp_client=None,
) -> tuple[str | None, str]:
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

    from .subagent_memory import SubagentMemoryContext
    from .subagent_registry import SubagentRegistry
    from .subagent_supplement import SubagentSupplementQueue

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

    # R5-P2：派发线程（主 Agent executor 线程）快照 source 到实例——
    # call_subagent 在 to_thread 线程读 runner._request_source 时主 Agent 可能已恢复默认
    _src_inst = SubagentRegistry.get(unique_name)
    if _src_inst is not None:
        from agent.runner import get_runner as _gr
        _src_inst.source = getattr(_gr(), "_request_source", "user")

    # 启动 asyncio task（主 Agent 在 executor 线程跑，必须用 run_coroutine_threadsafe 跨线程调度到主 loop）
    from niu_api.chat import _main_loop
    loop = _main_loop
    if loop is None or loop.is_closed():
        SubagentRegistry.unregister(unique_name)
        return (None, "[错误] 主 asyncio loop 不可用，无法派发异步子 Agent")

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
        return (None, f"[错误] 派发异步子 Agent 失败：{e}")

    # 回填 future 到注册表（用 future 而非 asyncio.Task，因为跨线程调度返回的是 concurrent.futures.Future）
    instance = SubagentRegistry.get(unique_name)
    if instance is not None:
        instance.task = future

    logger.info(f"[AsyncSubagent] 已派出异步子 Agent：{unique_name}")

    confirmation = (
        f"已派出子 Agent {unique_name}（类型：{agent_name}），后台运行中。\n"
        f"你可以用 check_subagent_progress('{unique_name}') 查看进度，\n"
        f"写 @ {unique_name} 消息给它补充上下文，\n"
        f"写 @ {unique_name} /stop 停止它。"
    )
    return (unique_name, confirmation)


async def _run_subagent_async(
    unique_name: str,
    agent_name: str,
    task: str,
    llm_config: dict[str, Any],
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
        # 用 last_reply（最后一轮输出）而非 result（所有轮次累加），避免中间过程挤占最终报告
        _inst = SubagentRegistry.get(unique_name)
        _last_reply = getattr(_inst, 'last_reply', '') if _inst else ''
        _result_for_notify = _last_reply if _last_reply else result
        completion_msg = f"[{unique_name}] 已完成，结果：{_result_for_notify}"
        try:
            get_main_agent_request_queue().push(completion_msg)
        except Exception as e:
            logger.error(f"[AsyncSubagent] {unique_name} 推完成通知失败：{e}")

        logger.info(f"[AsyncSubagent] {unique_name} 完成")

    except Exception as e:
        # 异常通知也推入 MainAgentRequestQueue
        err_str = str(e)
        err_msg = f"[{unique_name}] 异常结束：{err_str[:1000]}" + ("...[已截断]" if len(err_str) > 1000 else "")
        try:
            get_main_agent_request_queue().push(err_msg)
        except Exception:
            pass
        # 推送 subagent_error 事件到 SubagentEventBus（前端 tab 显示错误状态）
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            err_str_full = str(e)
            err_content = err_str_full[:2000] + ("...[已截断]" if len(err_str_full) > 2000 else "")
            notify_subagent_event_sync(unique_name, 'subagent_error', {'content': err_content})
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
        # 推送 subagent_error 事件到 SubagentEventBus
        try:
            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
            notify_subagent_event_sync(unique_name, 'subagent_error', {'content': '子 Agent 被取消'})
        except Exception:
            pass
        raise  # 重新抛出 CancelledError

    finally:
        # 清理 ask_main_agent pending future（避免泄漏）
        get_pending_ask_registry().unregister(unique_name)
        # 清理 ask_user pending future（避免 @user 阻塞期间 cancel 的边缘泄漏）
        try:
            from agent.ask_user import get_user_ask_registry
            get_user_ask_registry().unregister(unique_name)
        except ImportError:
            pass
        # 从注册表注销
        SubagentRegistry.unregister(unique_name)

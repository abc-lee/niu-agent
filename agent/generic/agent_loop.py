import json
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from agent.output_validator import validate_references
from agent.subagent import _read_warning_threshold

_VALID_STREAM_TYPES = ("reply", "tool_marker", "system", "persist")

_AT_NIU_PREFIX = "@niu-agent"  # 子 Agent 询问主 Agent 的 content 前缀（10 字符）
_AT_USER_PREFIX = "@user"  # 子 Agent 询问用户的 content 前缀（5 字符）

# 格式错误提示文本（用 f-string 插值 _AT_NIU_PREFIX，未来改名只改常量）
_FORMAT_ERROR_PROMPT = (
    "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
    f"1. 询问主 Agent：content 中包含 `{_AT_NIU_PREFIX}`，如 `{_AT_NIU_PREFIX} 我应该选择哪个选项？`\n"
    "2. 询问用户：content 中包含 `@user`，如 `@user 你需要哪个文件？`\n"
    "3. 结束会话：content 中包含 `@end`，如 `@end 任务已完成，结果：...`\n"
    "禁止输出不带 @ 前缀的纯 content。请重新输出。"
)

# @前缀子Agent意图识别返回值
INTERCEPTED = "intercepted"          # 异步 @niu-agent 拦截成功
INTERCEPTED_SYNC = "intercepted_sync"  # 同步 @niu-agent 拦截成功
EXIT = "exit"                        # @end 允许退出
FORMAT_ERROR = "format_error"        # 无 @ 前缀无 tool_calls，已追加格式错误提示
NO_INTERCEPTION = "no_intercept"     # 不拦截（主 Agent 或有 tool_calls）
INTERCEPTED_ASK_USER = "intercepted_ask_user"  # @user 拦截成功


def _find_unescaped_marker(content: str, marker: str) -> int:
    """在 content 里查找未转义标记的位置。

    规则（简单转义判断）：
    - 标记前一个紧邻字符是 `\\` → 不识别（转义），继续向后找
    - 其他位置（开头、中间、被反引号/引号包装等）→ 识别

    Args:
        content: 待搜索的文本（已 lstrip 或原始均可）
        marker: 要查找的标记（如 "@end" / "@niu-agent"）

    Returns:
        标记在 content 里的起始 index；未找到返回 -1。

    Examples:
        >>> _find_unescaped_marker("@end 任务完成", "@end")
        0
        >>> _find_unescaped_marker("`@end 任务完成`", "@end")
        1
        >>> _find_unescaped_marker("blah @end blah", "@end")
        5
        >>> _find_unescaped_marker(r"\\@end 任务完成", "@end")
        -1
        >>> _find_unescaped_marker("没有标记", "@end")
        -1
    """
    start = 0
    while True:
        idx = content.find(marker, start)
        if idx == -1:
            return -1
        # 前一个紧邻字符是 \\ → 转义，跳过本次匹配，从 idx+1 继续找
        if idx > 0 and content[idx - 1] == "\\":
            start = idx + 1
            continue
        return idx


@dataclass
class StreamEvent:
    type: str
    content: str

    def __post_init__(self):
        if self.type not in _VALID_STREAM_TYPES:
            raise ValueError(f"Invalid StreamEvent type: {self.type!r}, must be one of {_VALID_STREAM_TYPES}")

    def __str__(self):
        return self.content

    def __add__(self, other):
        if isinstance(other, str):
            return self.content + other
        if isinstance(other, StreamEvent):
            return self.content + other.content
        return NotImplemented

    def __radd__(self, other):
        if isinstance(other, str):
            return other + self.content
        return NotImplemented


def _intercept_at_prefix_content(
    content: str,
    tool_calls: list,
    messages: list,
    handler,
    memory_context,
) -> tuple:
    """@前缀子Agent意图识别拦截层。返回 (status, payload)。

    - (NO_INTERCEPTION, None)：主 Agent 或有 tool_calls，不拦截
    - (INTERCEPTED, None)：异步 @niu-agent 已处理（messages 已 append assistant + user）
    - (INTERCEPTED_SYNC, wrapped_text)：同步 @niu-agent，agent_runner_loop yield reply + return
    - (EXIT, None)：@end，agent_runner_loop 剥前缀 yield reply + return
    - (FORMAT_ERROR, None)：格式错误，agent_runner_loop continue

    Args:
        content: LLM 返回的 content
        tool_calls: LLM 返回的 tool_calls
        messages: 当前对话 messages 列表（会被追加）
        handler: NiuHandler 实例（含 _subagent_unique_name, _is_sync_subagent）
        memory_context: 异步子 Agent 的 memory_context（同步子 Agent 为 None）

    Returns:
        (status, payload) tuple
    """
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
    # tool_calls 时不拦截（正常工具调用）
    if tool_calls:
        return (NO_INTERCEPTION, None)

    # 一轮出方案的子 Agent（context-manager 模式二/三）绕过@前缀拦截：
    # 它们直接输出 keep=/update=/cursor= 让程序写数据库，拦截会导致正确输出被 FORMAT_ERROR，
    # 且追问引发的第二轮会把全量消息再发一遍，造成上下文溢出。
    # 由调用方经 call_subagent(bypass_at_prefix=True) 显式开启；模式一（多轮工具）不开启，
    # 走标准 @end/FORMAT_ERROR 结束判断。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    # 必须 is True 严格判断：测试常用 MagicMock handler，其同名属性是 truthy mock 对象，
    # 宽松判断会把所有 mock handler 误判为绕过，令 test_at_prefix_interception.py 大批失败。
    if getattr(handler, "_bypass_at_prefix", False) is True:
        return (NO_INTERCEPTION, None)

    stripped = (content or "").lstrip()

    # 主 Agent 分支：检测 content 误回复同步挂起子 Agent
    # 主 Agent 特征：memory_context is None and not is_sync_subagent
    # 误回复模式：content 以 @<同步挂起子名> 开头但本轮没调 chat-with 工具
    if memory_context is None and not is_sync_subagent:
        if _check_main_agent_content_reply_to_suspended(stripped, messages):
            return (FORMAT_ERROR, None)
        return (NO_INTERCEPTION, None)

    # 子 Agent 拦截（原逻辑）：@niu-agent / @end / 格式错误
    # @end 优先级最高：子 Agent 输出 @end 表示工作结束，无论是否同时包含
    # @niu-agent 或 @user，都直接退出。已经结束的子 Agent 再处理提问无意义。
    if _find_unescaped_marker(stripped, "@end") >= 0:
        return (EXIT, None)

    # @niu-agent 检测（子 Agent 向主 Agent 提问，阻塞等待回答）
    at_niu_idx = _find_unescaped_marker(stripped, _AT_NIU_PREFIX)
    if at_niu_idx >= 0:
        # 剥除 "@niu-agent" 前缀 + 可选空格（question 是 @niu-agent 之后的内容）
        question = stripped[at_niu_idx + len(_AT_NIU_PREFIX):].lstrip()
        if not question:
            logger.error(f"[AtPrefix] {_AT_NIU_PREFIX} 后无问题内容")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
            return (FORMAT_ERROR, None)
        # 超长检查：不截断，退回 FORMAT_ERROR 让子 Agent 精简后重新提问
        if len(question) > 2000:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"[输出超限额] 你的问题超过 2000 字符（当前 {len(question)} 字符），请精简后重新提问。如果无法精简，请用 @end 退出并说明原因。"})
            return (FORMAT_ERROR, None)

        unique_name = getattr(handler, "_subagent_unique_name", "")
        if not unique_name:
            logger.error("[AtPrefix] 子 Agent 无 _subagent_unique_name，无法调 ask_main_agent")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
            return (FORMAT_ERROR, None)

        if is_sync_subagent:
            # 同步路径：不阻塞，程序包装 [unique_name] question 返回
            from agent.subagent import _ask_main_agent_impl_sync
            wrapped = _ask_main_agent_impl_sync(
                question=question,
                unique_name=unique_name,
                handler=handler,
                messages=messages,
                content=content,
            )
            return (INTERCEPTED_SYNC, wrapped)
        else:
            # 异步路径：阻塞等主 Agent 回答（现有逻辑）
            from agent.subagent import _ask_main_agent_impl
            answer = _ask_main_agent_impl(
                question=question,
                unique_name=unique_name,
            )
            # 把 assistant content + 主 Agent 回答作为 user 消息注入 messages
            # 用 user 消息而非 tool 消息，避免 LLM API 对 tool_call_id 的严格校验
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"[主 Agent 回答] {answer}"})
            return (INTERCEPTED, None)

    # @user 检测（子 Agent 向用户提问，阻塞等待回答）
    at_user_idx = _find_unescaped_marker(stripped, _AT_USER_PREFIX)
    if at_user_idx >= 0:
        after_marker = at_user_idx + len(_AT_USER_PREFIX)
        # 检查 @user 后面是空白、常见标点或字符串结尾（词边界）
        if after_marker >= len(stripped) or stripped[after_marker] in (' ', '\t', '\n', ':', ',', '：', '，', '；', ';', '.', '。', '?', '？', '!', '！', '-', '/', ')', ']'):
            question = stripped[after_marker:].strip()
            if not question:
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
                return (FORMAT_ERROR, None)
            return (INTERCEPTED_ASK_USER, question)
        # @user 后面紧跟非空白字符（如 @username），不拦截，继续走到格式错误

    # 格式错误
    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
    return (FORMAT_ERROR, None)


def _check_main_agent_content_reply_to_suspended(stripped_content: str, messages: list) -> bool:
    """检测主 Agent content 是否在误回复同步挂起子 Agent。

    误回复模式：content 以 `@<子名> ` 开头，且 <子名> 在 SubagentRegistry 中
    且 state=waiting_for_answer 且 is_sync=True。

    支持两种子名格式（兼容 LLM 复读历史 hex 后缀格式）：
    - 同步路径：browser-operator（方案 B 后默认格式）
    - 异步路径旧格式：browser-operator-708b（LLM 复读 000045 等历史日志格式时出现）

    命中时：append assistant content + user 错误提示，返回 True。
    未命中：返回 False（不拦截）。
    """
    if not stripped_content or not stripped_content.startswith("@"):
        return False

    # 提取 @后的子名（只匹配字母数字下划线连字符，遇到中文标点等非 ASCII 立即停止）
    # 避免 @browser-operator。我选择 提取到 browser-operator。我选择 的错误
    m = re.match(r"@([A-Za-z0-9_\-]+)", stripped_content)
    if not m:
        return False
    target = m.group(1)

    # strip 非字母数字尾部（处理 LLM 误加英文标点）
    target_clean = target.rstrip(".,!?;:")

    from agent.subagent_registry import SubagentRegistry
    instance = SubagentRegistry.get(target_clean)

    # 兜底：target 含 hex 后缀旧格式（如 browser-operator-708b）时，提取 agent_type 再查
    # 兼容 LLM 复读历史日志格式的场景（000045 真实日志主 Agent 误回复就是 hex 后缀格式）
    if instance is None:
        hex_match = re.match(r"^(.+)-[0-9a-f]{4}$", target_clean)
        if hex_match:
            agent_type_candidate = hex_match.group(1)
            instance = SubagentRegistry.get(agent_type_candidate)
            if instance is not None:
                target_clean = agent_type_candidate  # 用真实 unique_name 更新

    if instance is None:
        return False  # 不在注册表，不拦截

    # 只拦截同步挂起 session（异步 running 走 db_monitor 原逻辑）
    if getattr(instance, "state", "running") != "waiting_for_answer":
        return False
    if not getattr(instance, "is_sync", True):
        return False

    # 命中误回复模式：append 错误提示，返回 FORMAT_ERROR
    agent_type = instance.agent_type
    error_prompt = (
        f"[对话格式错误] 你刚才用 content 文本回复了同步子 Agent {target_clean}，"
        f"这会导致它永久挂起。同步子 Agent 询问必须用工具回复。\n\n"
        f"请立即调用 chat-with-{agent_type} 工具，参数：\n"
        f"- task: \"\"（空字符串）\n"
        f"- answer: 你刚才想回复的内容（如 \"@{agent_type} 我选择 2\"）\n"
        f"- unique_name: 可省略（默认用 {agent_type}）\n\n"
        f"禁止再用 content 文本回复。"
    )
    messages.append({"role": "assistant", "content": stripped_content})
    messages.append({"role": "user", "content": error_prompt})
    logger.info(f"[AtPrefix] 主 Agent content 误回复同步挂起子 Agent {target_clean}，注入 FORMAT_ERROR 提示")
    return True


def format_subagent_supplement(items: list, is_final_position: bool = False) -> str:
    """格式化子 Agent supplement 为插入 LLM 上下文的文本。

    is_final_position=False（次末位）：普通补充，格式为"[发送者 补充] 内容"，跳过 terminate 项
    is_final_position=True（最末位）：/stop 终止，格式为终止指令文本
    """
    if not items:
        return ""

    if is_final_position:
        return "收到终止指令，请总结本轮工作后终止，不要再调用工具。"

    # 普通补充（跳过 terminate 项）
    parts = []
    for item in items:
        if getattr(item, "is_terminate", False):
            continue  # 终止指令不在次末位处理
        sender = getattr(item, "sender", "主Agent")
        content = getattr(item, "content", "")
        parts.append(f"[{sender} 补充] {content}")
    return "\n".join(parts) if parts else ""




def count_messages_tokens(messages: list) -> int:
    """
    估算消息列表的 token 数量

    使用 TokenCalculator，回退到字符数估算。
    """
    try:
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_messages(messages)
    except Exception:
        total = 0
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            # 兼容 list 格式 content（Claude cache_control 模式）
            # 用 " ".join 与 TokenCalculator 主路径一致
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            total += max(1, len(content) // 2) + 4
        return total


@dataclass
class StepOutcome:
    data: Any
    next_prompt: str | None = None
    should_exit: bool = False


def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, "__iter__") and not isinstance(ret, (str, bytes, dict, list)):
        ret = yield from ret
    return ret


class BaseHandler:
    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def dispatch(self, tool_name, args, response, index=0):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args["_index"] = index
            yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
            return ret
        elif tool_name == "bad_json":
            return StepOutcome(None, next_prompt=args.get("msg", "bad_json"), should_exit=False)
        else:
            yield StreamEvent("system", f"未知工具: {tool_name}\n")
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}", should_exit=False)


def json_default(o):
    if isinstance(o, set):
        return list(o)
    return str(o)


def exhaust(g):
    try:
        while True:
            next(g)
    except StopIteration as e:
        return e.value


def get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy()
        data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False).replace("\\n", "\n")


def _fifo_prune(messages, target_tokens, protect_recent_count=10, is_resumed=False):
    """FIFO 裁剪：按轮次组从 messages 头部开始删除，直到 token 数低于 target。
    一个轮次组 = assistant(+tool_calls?) -> tool* -> user(next_prompt)

    Args:
        messages: messages list（会被原地修改）
        target_tokens: 目标 token 数
        protect_recent_count: 保护最近 N 条消息不被裁剪（默认 10）
        is_resumed: 是否 resumed_messages 路径。True 时保护边界为
            messages[0]（system）+ 最近 protect_recent_count 条；
            False 时保持现有行为（保护 messages[0]+messages[1]，即
            system + 初始 user）。
    返回删除的消息数。
    """
    if len(messages) <= 2:
        return 0
    # 计算保护边界 protect_end：[0, protect_end) 是受保护区，从 protect_end 开始 FIFO 删除
    if is_resumed:
        protect_end = max(2, len(messages) - protect_recent_count)
    else:
        protect_end = 2
    removed = 0
    current_tokens = count_messages_tokens(messages)
    while len(messages) > protect_end and current_tokens > target_tokens:
        batch_removed = 0
        i = protect_end  # 始终从 protect_end 删除

        # 1. 删除 assistant（纯文本或 tool_calls）
        if i < len(messages) and messages[i].get("role") == "assistant":
            first = messages.pop(i)
            batch_removed += 1
            # 连带删除后续 tool 消息
            if first.get("tool_calls"):
                while i < len(messages) and messages[i].get("role") == "tool":
                    messages.pop(i)
                    batch_removed += 1

        # 2. 删除组末尾的 user（next_prompt），连带后续 assistant+tool*
        if i < len(messages) and messages[i].get("role") == "user":
            messages.pop(i)
            batch_removed += 1
            # 连带删除该 user 对应的 assistant 回复
            if i < len(messages) and messages[i].get("role") == "assistant":
                first = messages.pop(i)
                batch_removed += 1
                if first.get("tool_calls"):
                    while i < len(messages) and messages[i].get("role") == "tool":
                        messages.pop(i)
                        batch_removed += 1

        # 3. 保底：如果本轮没删任何消息（意外角色如孤立 tool），强制删 messages[protect_end]
        if batch_removed == 0 and len(messages) > protect_end:
            orphan = messages.pop(protect_end)
            batch_removed = 1
            # 孤立 tool 消息：连带删后续连续 tool
            if orphan.get("role") == "tool":
                while len(messages) > protect_end and messages[protect_end].get("role") == "tool":
                    messages.pop(protect_end)
                    batch_removed += 1

        removed += batch_removed
        current_tokens = count_messages_tokens(messages)
    return removed


MAX_TOOL_RESULT_CHARS = 30000  # 单个工具结果最大字符数（约 15K-30K token）
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200000  # 单消息内 tool 结果合计上限（参考 Claude Code）


def _truncate_tool_content(content: str, tool_name: str = "") -> str:
    """截断超长工具输出，保留开头部分并添加截断标记。"""
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    label = f"工具 {tool_name}" if tool_name else "工具"
    marker = f"\n\n[截断] {label}原始输出 {len(content)} 字符，已截断至 {MAX_TOOL_RESULT_CHARS} 字符。如需完整内容，请调整查询参数或分页重新获取。"
    truncated = content[:MAX_TOOL_RESULT_CHARS - len(marker)]
    return truncated + marker


def _truncate_dict_result(result, tool_name: str = ""):
    """对 dict 或任意对象做保底截断。

    dict 结果（如 lightrag_get_graph 返回的 {center, nodes, edges, stats}）
    序列化后可能超 MAX_TOOL_RESULT_CHARS。本函数：
    - 小 dict：原样返回
    - 大 dict：返回 {"status": "truncated", "message": "...", "data": 截断后的字符串}
    - 非 dict（不可序列化）：降级用 str() 后调 _truncate_tool_content

    这样既保留 dict 语义（status 检查），又避免超大结果进 messages。
    """
    try:
        serialized = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        # 不可序列化，降级为 str 截断
        return _truncate_tool_content(str(result), tool_name)

    if len(serialized) <= MAX_TOOL_RESULT_CHARS:
        return result  # 原样返回 dict

    # 超限：返回截断提示 dict
    label = f"{tool_name} " if tool_name else ""
    message = f"[截断] {label}原始输出 {len(serialized)} 字符，已截断至 {MAX_TOOL_RESULT_CHARS} 字符。如需完整内容，请调整查询参数（如缩小 depth/limit）或分页重新获取。"
    # 逐步缩减 data 直到整个 dict 序列化后 <= MAX_TOOL_RESULT_CHARS
    # （data 内可能含 " 等 JSON 特殊字符，被 json.dumps 转义后体积会膨胀，
    #   因此不能仅按 serialized 的字符数算，必须用 json.dumps 整体校验）
    budget = MAX_TOOL_RESULT_CHARS - len(message) - 200  # 给 status/message/结构开销留余量
    truncated_data = serialized[:budget]
    while True:
        candidate = {
            "status": "truncated",
            "message": message,
            "data": truncated_data,
        }
        if len(json.dumps(candidate, ensure_ascii=False)) <= MAX_TOOL_RESULT_CHARS:
            return candidate
        # 超限：继续砍 100 字符直到满足（保守，避免死循环）
        truncated_data = truncated_data[:-100] if len(truncated_data) > 100 else ""
        if not truncated_data:
            return candidate  # 极端情况：data 空也超限（message 过长），直接返回


def _enforce_message_budget(messages: list) -> list:
    """单消息内 tool 结果合计超 MAX_TOOL_RESULTS_PER_MESSAGE_CHARS 时，截断最大的几个。

    参考 Claude Code enforceToolResultBudget：防止一轮内多个并行工具结果
    合计爆掉单消息上限（火山方舟 'max message tokens'）。

    策略：按 tool content 大小降序，依次截断最大的，直到合计 <= 上限。
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    if not tool_indices:
        return messages

    total = sum(len(messages[i].get("content", "")) for i in tool_indices)
    if total <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS:
        return messages  # 未超限

    # 按大小降序排列 tool 消息索引
    tool_indices_sorted = sorted(tool_indices, key=lambda i: len(messages[i].get("content", "")), reverse=True)

    # 依次截断最大的，直到合计 <= 上限
    current_total = total
    for idx in tool_indices_sorted:
        if current_total <= MAX_TOOL_RESULTS_PER_MESSAGE_CHARS:
            break
        content = messages[idx].get("content", "")
        # 截断到 MAX_TOOL_RESULT_CHARS（保底值），释放 (len(content) - MAX_TOOL_RESULT_CHARS) 字符
        if len(content) > MAX_TOOL_RESULT_CHARS:
            messages[idx] = {
                **messages[idx],
                "content": _truncate_tool_content(content, "aggregated"),
            }
            current_total -= (len(content) - MAX_TOOL_RESULT_CHARS)

    logger.warning(f"[MessageBudget] tool results total {total} > {MAX_TOOL_RESULTS_PER_MESSAGE_CHARS}, truncated largest to {current_total}")
    return messages


def agent_runner_loop(
    client,
    system_prompt: str = "",  # 向后兼容（system_message 优先）
    user_input=None,
    handler=None,
    tools_schema=None,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,  # Optional: list of {"role": "user/assistant", "content": str}
    on_turn_end=None,  # Optional: callback(messages, tools_schema, turn) -> tools_schema
    context_window_tokens=0,  # 0 means no limit check (backward compatible)
    context_fifo_threshold=0,  # 0 means no FIFO truncation; >0 means max token budget for sub-agents
    context_target_threshold=0,  # FIFO 裁剪目标 token 量
    on_context_high_usage=None,  # 主Agent超阈值回调；None=子Agent走FIFO
    enable_supplement=True,  # False for sub-agents to prevent stealing main agent's supplements
    system_message: dict | None = None,  # 已组装好的 system message（首轮即带 cache_control）
    supplement_drain=None,  # 子 Agent 传入自己的 drain 函数；None 时走全局 drain_supplement
    memory_context: Any | None = None,  # 阶段二新增：异步子 Agent 进度数据，None=主 Agent 路径不更新
    resumed_messages=None,  # 阶段四新增：挂起恢复路径，传入则跳过 messages 构造直接用
    on_before_llm=None,  # Optional: callback(messages, turn) called before each LLM call; modifies messages[0] in place
):
    from agent.runner import clear_stop, drain_supplement, is_stop_requested

    if resumed_messages is not None:
        # 回复路径：直接用挂起的 messages，跳过 system_message + history + user_input 构造
        messages = resumed_messages
    else:
        # Build messages: system + history + current user
        # system_message 优先（首轮即带 cache_control）；否则回退到 system_prompt 字符串
        if system_message is not None:
            messages = [system_message]
        else:
            messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history if provided
        if history:
            # 从 assistant 消息的 tool_calls 构建 tool_call_id → tool_name 映射
            # 用于截断标记中显示工具名（DB 不存 tool_name，需从关联的 assistant 消息提取）
            _tc_id_to_name: dict[str, str] = {}
            # 收集所有有效的 tool_call_id（压缩可能留下孤立的 tool 消息）
            _valid_tc_ids: set[str] = set()
            for msg in history:
                role = msg.get("role", "user")
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = tc.get("id", "")
                        tc_name = tc.get("function", {}).get("name", "")
                        if tc_id and tc_name:
                            _tc_id_to_name[tc_id] = tc_name
                        if tc_id:
                            _valid_tc_ids.add(tc_id)

            # 收集所有 tool 消息的 tool_call_id，用于验证 assistant tool_calls 完整性
            _tool_response_ids: set[str] = set()
            for msg in history:
                if msg.get("role") == "tool" and msg.get("tool_call_id"):
                    _tool_response_ids.add(msg["tool_call_id"])

            for msg in history:
                role = msg.get("role", "user")
                # === 过滤 subagent_msg 消息，不塞进 LLM 上下文（@ 消息仅供前端展示） ===
                if role == "subagent_msg":
                    continue
                content = msg.get("content", "")
                if role in ("user", "assistant") and (content or msg.get("tool_calls")):
                    entry = {"role": role, "content": content}
                    # 还原 tool_calls（assistant 消息可能携带工具调用）
                    if msg.get("tool_calls"):
                        # 过滤掉没有对应 tool 响应的 tool_calls（压缩可能删除了 tool 输出）
                        valid_tcs = [tc for tc in msg["tool_calls"] if tc.get("id") in _tool_response_ids]
                        if valid_tcs:
                            entry["tool_calls"] = valid_tcs
                        # 如果所有 tool_calls 都没有响应，不设置 tool_calls（变成纯文本消息）
                    messages.append(entry)
                elif role == "tool" and msg.get("tool_call_id") and content is not None:
                    # 跳过孤立的 tool 消息（没有对应的 assistant tool_calls）
                    if msg["tool_call_id"] not in _valid_tc_ids:
                        logger.warning(f"[AgentLoop] Skipping orphan tool message: tool_call_id={msg['tool_call_id']}")
                        continue
                    # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
                    # 截断超长的 tool 内容（DB 中保存了完整内容，但 LLM 上下文需要保护）
                    tool_name = _tc_id_to_name.get(msg["tool_call_id"], "")
                    entry = {"role": role, "content": _truncate_tool_content(content, tool_name), "tool_call_id": msg["tool_call_id"]}
                    if tool_name:
                        entry["name"] = tool_name
                    messages.append(entry)

        # Add current user message
        messages.append({
            "role": "user",
            "content": initial_user_content if initial_user_content is not None else user_input,
        })

    # Debug info only - logging is done in ToolClient.chat where the real prompt is built
    logger.info(f"[Debug] agent_runner_loop: {len(messages)} messages (history: {len(history) if history else 0})")

    turn = 0
    last_prompt_tokens = 0
    handler._last_prompt_tokens = 0
    _compress_cooldown = False  # 回调冷却：同一轮 agent_runner_loop 只触发一次压缩
    handler._done_hooks = []
    handler.max_turns = max_turns
    # V4: 通知前端进入忙碌状态
    yield StreamEvent("system", "chat_busy")

    _harness_fail_count = 0
    _max_harness_retries = 3
    warning_threshold = _read_warning_threshold()

    while turn < handler.max_turns:
        turn += 1
        # --- Stop flag check ---
        if is_stop_requested():
            logger.info("[AgentLoop] Stop requested, exiting loop")
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}
        # === 上下文使用率检测（prompt_tokens 驱动）===
        if last_prompt_tokens > 0 and context_window_tokens > 0 and not _compress_cooldown:
            usage_ratio = last_prompt_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                if on_context_high_usage:
                    # 主 Agent：调回调执行压缩，循环不退出
                    logger.info(f"[Context] Proactive compress: {last_prompt_tokens}/{context_window_tokens} tokens "
                                f"({usage_ratio:.1%} > {warning_threshold:.0%})")
                    on_context_high_usage(messages, last_prompt_tokens, context_window_tokens)
                    # 回调内部已完成压缩并原地修改 messages（messages[:] = ...）
                    last_prompt_tokens = 0  # 重置，下轮重新获取
                    handler._last_prompt_tokens = 0
                    _compress_cooldown = True  # 冷却：本次 agent_runner_loop 不再触发压缩
                else:
                    # 子 Agent：FIFO 裁剪到 target 阈值
                    target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.30)
                    removed = _fifo_prune(messages, target_tokens, is_resumed=(resumed_messages is not None))
                    if removed > 0:
                        logger.info(f"[FIFO] Proactive pruning: {last_prompt_tokens}/{context_window_tokens} tokens "
                                    f"({usage_ratio:.1%} > {warning_threshold:.0%}), removed {removed} messages, "
                                    f"now ~{count_messages_tokens(messages)} tokens (target {target_tokens})")
            # 旧 FIFO 回退：只在首轮（last_prompt_tokens==0）时执行
        if context_fifo_threshold > 0 and len(messages) > 2 and last_prompt_tokens == 0 and not _compress_cooldown:
            removed = _fifo_prune(messages, context_fifo_threshold, is_resumed=(resumed_messages is not None))
            if removed > 0:
                logger.info(f"[FIFO] Fallback truncation: removed {removed} oldest messages, "
                            f"tokens {count_messages_tokens(messages)}/{context_fifo_threshold}")
        if verbose:
            yield StreamEvent("system", f"**LLM Running (Turn {turn}) ...**\n\n")
        if turn % 10 == 0:
            client.last_tools = ""  # 每10轮重置一次工具描述，避免上下文过大导致的模型性能下降
        # 单消息聚合上限检查（防多个 tool 结果合计爆掉单消息上限）
        messages = _enforce_message_budget(messages)
        # 阶段二：异步子 Agent 进度数据 — LLM 请求前更新 last_llm_request + current_turn
        # 取 messages 里最后一条 role==user 的 content 摘要（无 supplement 时本轮 user 是上一轮遗留的，倒序找正确）
        if memory_context is not None:
            try:
                last_user_content = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        content = m.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                block.get("text", "") if isinstance(block, dict) else str(block)
                                for block in content
                            )
                        last_user_content = str(content)[:500]  # 摘要前 500 字符
                        break
                memory_context.update(
                    last_llm_request=last_user_content,
                    current_turn=turn,
                )
            except Exception:
                pass  # 进度更新失败不影响主流程
        # 动态注入：每轮 LLM 调用前刷新 system message（skill/knowledge/脑区/habits）
        # 关键：必须在 client.chat 之前，让本轮 LLM 立即读到新 system message
        if on_before_llm is not None:
            try:
                on_before_llm(messages, turn)
            except Exception:
                logger.exception("[AgentLoop] on_before_llm callback failed")
        response_gen = client.chat(messages=messages, tools=tools_schema)
        if verbose:
            response = yield from response_gen
            yield StreamEvent("system", "\n\n")
        else:
            response = exhaust(response_gen)
            # 过滤掉 <tool_use> 标签，只返回纯文本
            content = response.content or ""
            content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)

            # 阶段三/四：@前缀子Agent意图识别拦截（异步+同步子 Agent）
            if not response.tool_calls:
                interception_status, interception_payload = _intercept_at_prefix_content(
                    content=content,
                    tool_calls=response.tool_calls,
                    messages=messages,
                    handler=handler,
                    memory_context=memory_context,
                )
                if interception_status == INTERCEPTED:
                    continue  # 异步路径：LLM 重跑（messages 已 append assistant + user）
                if interception_status == INTERCEPTED_ASK_USER:
                    question = interception_payload
                    unique_name = getattr(handler, '_subagent_unique_name', '')
                    if not unique_name:
                        continue  # 无 unique_name，跳过（不应发生）
                    from agent.subagent import _ask_user_impl
                    messages.append({"role": "assistant", "content": content})
                    answer = _ask_user_impl(question, unique_name)
                    if answer and answer != '__TERMINATED__':
                        messages.append({"role": "user", "content": f"[user 回答] {answer}"})
                    else:
                        messages.append({"role": "user", "content": "[user 未回答] 你的提问超时或被终止，请基于现有信息继续或用 @end 退出。"})
                    continue
                if interception_status == INTERCEPTED_SYNC:
                    # 同步路径：yield wrapped_text + 显式 return
                    # 子 Agent 路径不调全局 clear_stop()（避免清主 Agent stop 标志）
                    yield StreamEvent("reply", interception_payload)
                    yield StreamEvent("system", "chat_idle")
                    return {"result": "INTERCEPTED_SYNC", "messages": messages, "finish_reason": "intercepted_sync"}
                if interception_status == EXIT:
                    # @end 允许退出，剥除 "@end" 前缀 + 可选空格后推前端
                    stripped_content = content.lstrip()
                    at_end_idx = _find_unescaped_marker(stripped_content, "@end")
                    if at_end_idx >= 0:
                        exit_content = stripped_content[at_end_idx + 4:].lstrip()
                        # 边界：@end 恰好 4 字符时剥前缀后为空，用原始 content 避免前端收到空回复
                        if not exit_content:
                            exit_content = content
                    else:
                        exit_content = content
                    yield StreamEvent("reply", exit_content)
                    yield StreamEvent("system", "chat_idle")
                    return {"result": "EXITED", "messages": messages, "finish_reason": "exited"}
                if interception_status == FORMAT_ERROR:
                    _harness_fail_count = 0  # 重置，避免格式错误累计影响 validate_references
                    continue  # 格式错误，回到 while 循环让 LLM 重新输出
                # NO_INTERCEPTION：继续走原有逻辑

            # Harness 验证：仅在 LLM 不调工具直接回复用户时验证
            # 条件 not response.tool_calls 精确区分最终回复 vs 中间工具调用
            if not response.tool_calls:
                validation = validate_references(content)
                if not validation.is_valid and _harness_fail_count < _max_harness_retries:
                    _harness_fail_count += 1
                    feedback = validation.format_feedback()
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": feedback})
                    continue  # 回到 while 循环，让 LLM 修正
                _harness_fail_count = 0

            yield StreamEvent("reply", content)
            # 子 Agent thinking chain 推送（仅在非 verbose 分支内，verbose 分支不经过 reply yield）
            if getattr(handler, '_is_subagent', False):
                unique_name = getattr(handler, '_subagent_unique_name', None)
                if unique_name and hasattr(response, 'thinking') and response.thinking:
                    try:
                        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
                        notify_subagent_event_sync(unique_name, 'thinking_chain', {'content': response.thinking})
                    except ImportError:
                        pass

            # 阶段二：异步子 Agent 进度数据 — LLM 响应组装完后更新 last_llm_response
            # 位置：yield StreamEvent("reply", content) 之后（else/非 verbose 分支内，content 已在 L447 定义）
            if memory_context is not None:
                try:
                    memory_context.update(last_llm_response=(content or "")[:2000])
                except Exception:
                    pass

        # 统一提取 prompt_tokens（verbose/else 分支共用）
        if hasattr(response, 'usage') and response.usage:
            u = response.usage
            _pt = u.get('prompt_tokens', 0) if isinstance(u, dict) else getattr(u, 'prompt_tokens', 0)
            if isinstance(_pt, (int, float)):
                last_prompt_tokens = int(_pt)
                handler._last_prompt_tokens = last_prompt_tokens
                logger.info(f"[Context] prompt_tokens={last_prompt_tokens}, context_window={context_window_tokens}")
                # 提取后立即检测：如果超阈值，在当前轮就触发回调/FIFO
                # （无工具调用时循环会退出，下轮顶部检测不会执行，所以此处必须检测）
                if context_window_tokens > 0 and not _compress_cooldown:
                    usage_ratio = last_prompt_tokens / context_window_tokens
                    if usage_ratio > warning_threshold:
                        if on_context_high_usage:
                            logger.info(f"[Context] Proactive compress: {last_prompt_tokens}/{context_window_tokens} tokens "
                                        f"({usage_ratio:.1%} > {warning_threshold:.0%})")
                            on_context_high_usage(messages, last_prompt_tokens, context_window_tokens)
                            last_prompt_tokens = 0
                            handler._last_prompt_tokens = 0
                            _compress_cooldown = True
                        else:
                            target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.30)
                            removed = _fifo_prune(messages, target_tokens, is_resumed=(resumed_messages is not None))
                            if removed > 0:
                                logger.info(f"[FIFO] Proactive pruning: removed {removed} messages")
        else:
            logger.debug(f"[Context] No usage in response: hasattr={hasattr(response, 'usage')}, usage={getattr(response, 'usage', 'N/A')}")

        # 检测 LLM 返回的 context_length_exceeded 标记（覆盖 verbose=True 和 verbose=False）
        if hasattr(response, 'context_overflow') and response.context_overflow:
            logger.warning("[Overflow] LLM API returned context_length_exceeded, triggering CONTEXT_OVERFLOW")
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {
                "result": "CONTEXT_OVERFLOW",
                "data": {
                    "overflow": True,
                    "turns_completed": turn - 1,
                    "tokens_used": last_prompt_tokens if last_prompt_tokens > 0 else count_messages_tokens(messages),
                    "tokens_limit": context_window_tokens,
                },
                "messages": messages,
            }

        # 如果在 LLM 流式传输期间请求停止，跳过部分 tool_calls 处理
        if is_stop_requested():
            logger.info("[AgentLoop] Stop requested after LLM stream, skipping tool calls")
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}

        if not response.tool_calls:
            tool_calls = [{"tool_name": "no_tool", "args": {}}]
        else:
            # P0-6: 添加 JSON 解析异常处理
            tool_calls = []
            for tc in response.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    tool_calls.append({
                        "tool_name": tc.function.name,
                        "args": args,
                        "id": tc.id,
                    })
                except json.JSONDecodeError as e:
                    # 记录错误并使用空参数继续执行
                    logger.error(f"[ERROR] Failed to parse tool arguments for {tc.function.name}: {e}")
                    logger.error(f"[ERROR] Raw arguments: {tc.function.arguments}")
                    tool_calls.append({
                        "tool_name": tc.function.name,
                        "args": {},  # 回退为空参数
                        "id": tc.id,
                        "error": str(e),  # 记录错误信息
                    })

        # 添加assistant消息（如果有工具调用）
        if response.tool_calls:
            assistant_msg = {"role": "assistant", "content": response.content or "", "tool_calls": []}
            for tc in response.tool_calls:
                assistant_msg["tool_calls"].append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })
            messages.append(assistant_msg)
            # V4: yield persist事件，逐条持久化assistant(tool_calls)消息
            yield StreamEvent("persist", json.dumps(assistant_msg, ensure_ascii=False))

        tool_results = []
        next_prompts = set()
        should_exit = None
        # 注入当前消息列表到 handler，使子Agent能获取主Agent的对话历史
        # 注意：此时 messages 包含本轮的 assistant(tool_calls) 但不含 tool 结果
        handler._current_messages = messages
        for ii, tc in enumerate(tool_calls):
            # 阶段二：异步子 Agent 进度数据 — 工具调度时更新 last_tool_name
            # 位置：for 循环体开头，dispatch 调用前
            # tc 是 handler 预处理后的对象，用 tc["tool_name"] 取（与现有 L563 一致）
            if memory_context is not None:
                try:
                    tc_tool_name = tc.get("tool_name", "") if isinstance(tc, dict) else ""
                    if tc_tool_name:
                        memory_context.update(last_tool_name=tc_tool_name)
                except Exception:
                    pass
            tool_name, args, tid = tc["tool_name"], tc["args"], tc.get("id", "")
            if tool_name == "no_tool":
                continue
            elif verbose:
                showarg = get_pretty_json(args)
                yield StreamEvent("tool_marker", f"🛠️ **正在调用工具:** `{tool_name}`  📥**参数:**\n````text\n{showarg}\n````\n")
            handler.current_turn = turn
            # --- Stop flag check before tool dispatch ---
            if is_stop_requested():
                logger.info("[AgentLoop] Stop requested, skipping remaining tools")
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {"result": "STOPPED", "messages": messages}
            gen = handler.dispatch(tool_name, args, response, index=ii)
            if verbose:
                yield StreamEvent("tool_marker", "`````\n")
                outcome = yield from gen
                yield StreamEvent("tool_marker", "`````\n")
            else:
                outcome = exhaust(gen)

            # === 统一截断关口 ===
            # 距离 Agent 调用最近，覆盖所有工具路径（MCP/disk/内置/chat-with-*）
            # 前端 API 和内部业务（region_detector/region_manager）不经过 dispatch，不被截断
            if outcome.data is not None:
                if isinstance(outcome.data, dict):
                    outcome.data = _truncate_dict_result(outcome.data, tool_name)
                elif isinstance(outcome.data, list):
                    # list 类型：序列化后截断，返回 truncated dict（与 _truncate_dict_result 一致）
                    _list_str = json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                    if len(_list_str) > MAX_TOOL_RESULT_CHARS:
                        _label = f"工具 {tool_name}" if tool_name else "工具"
                        _message = f"[截断] {_label}原始输出 {len(_list_str)} 字符，已截断至 {MAX_TOOL_RESULT_CHARS} 字符。"
                        _budget = MAX_TOOL_RESULT_CHARS - len(_message) - 200
                        outcome.data = {
                            "status": "truncated",
                            "message": _message,
                            "data": _list_str[:_budget],
                        }
                elif isinstance(outcome.data, str):
                    outcome.data = _truncate_tool_content(outcome.data, tool_name)

            if outcome.should_exit:
                # should_exit路径：补齐当前tool_result到tool_results列表
                if tid:
                    if outcome.data is not None:
                        datastr = (
                            json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                            if type(outcome.data) in [dict, list]
                            else str(outcome.data)
                        )
                        tool_results.append({"tool_use_id": tid, "content": datastr, "tool_name": tool_name})
                    else:
                        tool_results.append({"tool_use_id": tid, "content": "", "tool_name": tool_name})
                # 添加tool消息到messages
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        # 冗余截断（统一关口已在 dispatch 后截断 outcome.data），保留作防御性编程
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                    }
                    _tn = tool_result.get("tool_name", "")
                    if _tn:
                        tool_msg["name"] = _tn
                    messages.append(tool_msg)
                # V4: yield每条tool结果的persist事件
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": tool_result["content"]
                    }
                    yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {
                    "result": "EXITED",
                    "data": outcome.data,
                    "messages": messages,
                }  # should_exit is only used for immediate exit
            if outcome.next_prompt.startswith("未知工具") or outcome.next_prompt.startswith("Unknown tool"):
                client.last_tools = ""

            # 关键：Anthropic API 要求每个 tool_call 都有 tool_result
            # 即使 outcome.data 为 None，也必须添加空的 tool_result
            # 但 no_tool 场景 tid 为空字符串，不应产生孤立的 tool 消息
            if tid:
                if outcome.data is not None:
                    datastr = (
                        json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                        if type(outcome.data) in [dict, list]
                        else str(outcome.data)
                    )
                    tool_results.append({"tool_use_id": tid, "content": datastr, "tool_name": tool_name})
                else:
                    tool_results.append({"tool_use_id": tid, "content": "", "tool_name": tool_name})

            next_prompts.add(outcome.next_prompt)

        # 添加tool消息（工具结果）
        for tool_result in tool_results:
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                # 冗余截断（统一关口已在 dispatch 后截断 outcome.data），保留作防御性编程
                "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
            }
            _tn = tool_result.get("tool_name", "")
            if _tn:
                tool_msg["name"] = _tn
            messages.append(tool_msg)
        # V4: yield每条tool结果的persist事件
        for tool_result in tool_results:
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": tool_result["content"]
            }
            yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))

        if len(next_prompts) == 0:
            if len(handler._done_hooks) == 0:
                # 纯文本回复：也要执行衰减
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                # V4: 纯文本回复yield persist事件（从response.content构造，不从messages[-1]获取）
                if response.content and not response.tool_calls:
                    pure_text_msg = {"role": "assistant", "content": response.content}
                    yield StreamEvent("persist", json.dumps(pure_text_msg, ensure_ascii=False))
                # V4: 通知前端进入空闲状态
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                if isinstance(should_exit, dict):
                    should_exit["messages"] = messages
                    return should_exit
                # should_exit 为 None 时（无工具调用），返回标准格式
                return {
                    "result": "CURRENT_TASK_DONE",
                    "data": None,
                    "messages": messages,
                    "finish_reason": response.finish_reason if response else None,
                }
            next_prompts.add(handler._done_hooks.pop(0))
        next_prompt = handler.next_prompt_patcher("\n".join(next_prompts), None, turn)

        # --- 见缝插针：读取用户在 Agent 运行期间发送的补充消息 ---
        # drain 必须在 response.tool_calls 退出检查之前完成，以便：
        # 1. 终止指令能强制退出循环（无论 LLM 是否调工具）
        # 2. 补充消息能在 LLM 决定退出时仍被注入
        supplement_terminate = False
        supplement = None
        if enable_supplement:
            drain_fn = supplement_drain if supplement_drain is not None else drain_supplement
            drained = drain_fn()
            # 主 Agent 路径：返回 str | None
            if isinstance(drained, str) or drained is None:
                supplement = drained
            # 子 Agent 路径：返回 list[SubagentSupplementItem]
            elif isinstance(drained, list):
                has_terminate = any(getattr(item, "is_terminate", False) for item in drained)
                if has_terminate:
                    supplement = format_subagent_supplement(drained, is_final_position=True)
                    supplement_terminate = True
                else:
                    supplement = format_subagent_supplement(drained, is_final_position=False)

        # 终止模式下：调 LLM 生成总结后退出（方案 B'）
        if supplement_terminate:
            logger.warning("[AgentLoop] 终止模式下调用 LLM 生成总结后退出")
            # 注意：on_turn_end 已在上方工具调用后调用过，此处不再重复调用（避免重复衰减——风险2）
            # 不 yield chat_idle（保持 busy 状态——风险1）
            # 1. 把 supplement 文本作为 user 消息追加到 messages（创建新列表，不污染调用方传入的列表）
            messages = messages + [{"role": "user", "content": supplement}]
            # 2. 调 LLM 生成总结（tools=[] 强制无工具调用）
            summary_text = ""
            summary_response = None
            try:
                summary_gen = client.chat(messages=messages, tools=[])
                summary_response = exhaust(summary_gen)
                if summary_response and hasattr(summary_response, "content") and summary_response.content:
                    summary_text = summary_response.content
                # 3. persist 总结（复用现有纯文本 persist 模式）
                if summary_text:
                    yield StreamEvent("reply", summary_text)
                    yield StreamEvent("persist", json.dumps({
                        "role": "assistant",
                        "content": summary_text
                    }, ensure_ascii=False))
            except Exception as e:
                # 风险3：LLM 调用失败兜底，仍返回 TERMINATED_BY_SUPPLEMENT
                logger.error(f"[AgentLoop] 终止模式下生成总结失败：{e}")
            # 4. return TERMINATED_BY_SUPPLEMENT
            # 注意：子 Agent 路径不在此清除停止信号灯——避免误清主 Agent 信号灯。
            # 主 Agent 会在自己的退出逻辑里清信号灯（见下方 not response.tool_calls 分支）。
            yield StreamEvent("system", "chat_idle")
            return {
                "result": "TERMINATED_BY_SUPPLEMENT",
                "data": None,
                "messages": messages,
                "finish_reason": summary_response.finish_reason if summary_response else None,
            }

        # 退出逻辑：LLM 无工具调用时退出（纯文本回复 = 任务完成或等待用户输入）
        if not response.tool_calls:
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            if isinstance(should_exit, dict):
                should_exit["messages"] = messages
                return should_exit
            return {
                "result": "CURRENT_TASK_DONE",
                "data": None,
                "messages": messages,
                "finish_reason": response.finish_reason,
            }

        # 警告注入：只在有工具调用时才有意义（LLM 还在工作，可能需要调整策略）
        # 补充消息插在 next_prompt 前面，当前任务作为最后一条，LLM 优先处理
        if supplement or (next_prompt and next_prompt.strip()):
            combined = ""
            if supplement:
                combined = supplement
            if next_prompt and next_prompt.strip():
                combined = combined + "\n" + next_prompt if combined else next_prompt
            messages.append({"role": "user", "content": combined})
            if supplement:
                logger.info(f"[AgentLoop] Supplement inserted before next_prompt: {supplement[:80]}...")

        # 轮次级刷新回调：允许调用方在每轮结束后更新 system_prompt 和 tools_schema
        if on_turn_end is not None:
            tools_schema = on_turn_end(messages, tools_schema, turn)

    # MAX_TURNS_EXCEEDED 退出时也要执行衰减
    if on_turn_end is not None:
        on_turn_end(messages, tools_schema, turn)
    # V4: 通知前端进入空闲状态
    clear_stop()
    yield StreamEvent("system", "chat_idle")
    return {"result": "MAX_TURNS_EXCEEDED", "messages": messages}

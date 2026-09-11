import json
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Callable

from agent.tmp_dir import get_tmp_dir
from loguru import logger

from agent.output_validator import validate_references
from agent.subagent import _read_warning_threshold

# 统一压缩入口（spec 2026-09-06）：发送前门裸用意图 API（R6-B P2-1：模块级导入防 NameError）
from agent.compression_intent import consume_compression, request_compression, peek_compression, reset_compression_intent

_VALID_STREAM_TYPES = ("reply", "tool_marker", "system", "persist")

_AT_NIU_PREFIX = "@niu-agent"  # 子 Agent 询问主 Agent 的 content 前缀（10 字符）
_AT_USER_PREFIX = "@user"  # 子 Agent 询问用户的 content 前缀（5 字符）

# 格式错误提示文本（用 f-string 插值 _AT_NIU_PREFIX，未来改名只改常量）
# T1（2026-08-15）：@ 整段传递——完整上下文 + @niu-agent 提问
_FORMAT_ERROR_PROMPT = (
    "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
    f"1. 询问主 Agent：把完整上下文写在 `{_AT_NIU_PREFIX}` 前后，用 `{_AT_NIU_PREFIX}` 标注提问（如 `{_AT_NIU_PREFIX} 我应该选择哪个选项？`）——主 Agent 会看到整段\n"
    "2. 询问用户：把完整上下文写在 `@user` 前，用 `@user` 标注提问（如 `@user 你需要哪个文件？`）——用户会看到整段\n"
    "3. 结束会话：把汇报内容写在 `@end` 前（如 `任务已完成，结果：... @end`）——主 Agent 会收到完整汇报\n"
    "禁止输出不带 @ 前缀的纯 content。请重新输出。"
)

# @前缀子Agent意图识别返回值
INTERCEPTED = "intercepted"          # 异步 @niu-agent 拦截成功
INTERCEPTED_SYNC = "intercepted_sync"  # 同步 @niu-agent 拦截成功
EXIT = "exit"                        # @end 允许退出
FORMAT_ERROR = "format_error"        # 无 @ 前缀无 tool_calls，已追加格式错误提示
NO_INTERCEPTION = "no_intercept"     # 不拦截（主 Agent 或有 tool_calls）
INTERCEPTED_ASK_USER = "intercepted_ask_user"  # @user 拦截成功

# @指令跳过提示文案（2026-09-03 D1-D4）：@指令与工具调用同轮时工具优先执行、@指令被静默跳过，
# 下轮提示要求将完整内容与指令一起单独重发——裸指令会命中空问题守卫/丢最终汇报
_SKIPPED_AT_NIU_PROMPT = (
    "[系统提示] 你上一轮的输出同时包含 @niu-agent 提问和工具调用——同一轮中工具调用优先执行，"
    "@niu-agent 提问未送达主 Agent。如需提问，请在下一轮将完整提问与 @niu-agent 一起单独输出（不带工具调用）。"
)
_SKIPPED_AT_END_PROMPT = (
    "[系统提示] 你上一轮的输出同时包含 @end 和工具调用——工具调用优先执行，@end 结束指令未生效。"
    "工作完成后，请在下一轮将最终汇报与 @end 一起单独输出（不带工具调用）。"
)
_SKIPPED_AT_USER_PROMPT = (
    "[系统提示] 你上一轮的输出同时包含 @user 提问和工具调用——同一轮中工具调用优先执行，"
    "@user 提问未生效。如需向用户提问，请在下一轮将完整提问与 @user 一起单独输出（不带工具调用）。"
)


def _detect_skipped_at_directive(content: str) -> str | None:
    """检测 content 中因同轮工具调用而被跳过的未转义 @指令（2026-09-03 D1，纯函数供单测直打）。

    优先级 @end → @niu-agent → @user（与拦截层一致：@end 最高），命中首个即返回对应提示文案（§4），
    无命中返回 None。@user 词边界复刻拦截层条件（后跟空白/常见标点/串尾才识别——防 @username 误判）；
    @end/@niu-agent 无需边界（拦截层本身无边界检查，保持过匹配一致）。

    Args:
        content: LLM 本轮响应 content（可为 None/空串）

    Returns:
        命中的提示文案字符串；无未转义 @指令返回 None。
    """
    text = content or ""
    if _find_unescaped_marker(text, "@end") >= 0:
        return _SKIPPED_AT_END_PROMPT
    if _find_unescaped_marker(text, _AT_NIU_PREFIX) >= 0:
        return _SKIPPED_AT_NIU_PROMPT
    at_user_idx = _find_unescaped_marker(text, _AT_USER_PREFIX)
    if at_user_idx >= 0:
        after_marker = at_user_idx + len(_AT_USER_PREFIX)
        # 词边界：后跟空白/常见标点/串尾才识别（复刻拦截层条件——防 @username 误判）
        if after_marker >= len(text) or text[after_marker] in (' ', '\t', '\n', ':', ',', '：', '，', '；', ';', '.', '。', '?', '？', '!', '！', '-', '/', ')', ']'):
            return _SKIPPED_AT_USER_PROMPT
    return None


def _find_unescaped_marker(content: str, marker: str) -> int:
    """在 content 里查找未转义标记的位置（大小写不敏感——Agent 可能输出 @END/@NIU-AGENT/@USER 等大写形式）。

    规则（简单转义判断）：
    - 标记前一个紧邻字符是 `\\` → 不识别（转义），继续向后找
    - 其他位置（开头、中间、被反引号/引号包装等）→ 识别

    实现：用 content.lower() 与 marker.lower() 做 find；idx 在 lower 字符串与原始字符串中一致
    （.lower() 不改变 ASCII 长度），转义判断仍用原始 content（content[idx-1]）。

    Args:
        content: 待搜索的文本（已 lstrip 或原始均可）
        marker: 要查找的标记（如 "@end" / "@niu-agent"）

    Returns:
        标记在 content 里的起始 index；未找到返回 -1。

    Examples:
        >>> _find_unescaped_marker("@end 任务完成", "@end")
        0
        >>> _find_unescaped_marker("@END 任务完成", "@end")
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
    lower_content = content.lower()
    lower_marker = marker.lower()
    start = 0
    while True:
        idx = lower_content.find(lower_marker, start)
        if idx == -1:
            return -1
        # 前一个紧邻字符是 \\ → 转义，跳过本次匹配，从 idx+1 继续找（idx 在 lower 与原始字符串中一致）
        if idx > 0 and content[idx - 1] == "\\":
            start = idx + 1
            continue
        return idx


def _compute_exit_content(stripped: str, at_end_idx: int, content: str) -> str:
    """计算 @end 退出内容：@end 标记前 + @end 标记后拼接（标记本身剥掉）。

    T1（2026-08-15）：@end 边界修复——原实现 `stripped[at_end_idx + 4:].lstrip()`
    只取 @end 后内容，@end 在 content 中间时前半主体被丢弃。改为前 + 后整段保留。

    - @end 在末尾 → 返回 @end 前完整内容（尾部空白归一——无尾随空格）
    - @end 在中间 → 前 + 后拼接（标记剥掉 + 段间空白归一为单空格）
    - 拼接结果为空或纯空白（"@end" / "@end\n" 形态）→ 兜底返回原始 content（与历史行为一致）

    P3（2026-08-15）：拼接两段做 rstrip/lstrip 归一——`f"{before.rstrip()} {after.lstrip()}".strip()`
    语义：双空格/尾随空格/前导空格统一为单空格；纯空白结果（如 "@end\n" → "\n"）不再绕过空值兜底。

    Args:
        stripped: 已 lstrip 的 content
        at_end_idx: @end 标记在 stripped 中的起始 index（_find_unescaped_marker 返回值）
        content: 原始 content（空值兜底用——以实码为准，main 现有兜底是 content 非 stripped）

    Returns:
        退出内容字符串
    """
    before = stripped[:at_end_idx].rstrip()
    after = stripped[at_end_idx + 4:].lstrip()
    exit_content = f"{before} {after}".strip()
    if not exit_content:
        return content
    return exit_content


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

    # 一轮出方案的子 Agent 绕过@前缀拦截（T6 前 context-manager 模式二/三是唯一使用者，
    # 该 Agent 已随压缩体系退役；机制保留供未来一轮出方案型子 Agent 复用）：
    # 拦截会导致正确输出被 FORMAT_ERROR，且追问引发的第二轮会把全量消息再发一遍。
    # 由调用方经 call_subagent(bypass_at_prefix=True) 显式开启；多轮工具型子 Agent 不开启，
    # 走标准 @end/FORMAT_ERROR 结束判断。
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
        # T1（2026-08-15）：整段传递——question = 完整 stripped
        # （@ 前上下文 + @niu-agent + @ 后提问全部传给主 Agent——含标记原样保留；
        # @niu-agent 是收件人称呼属整段内容，与 @end（终止控制符剥掉）处理不同）
        question = stripped
        # 空问题守卫保留：@niu-agent 标记后无问题内容 → 仍 FORMAT_ERROR
        # （裸 @niu-agent 即时纠错不阻塞 300s——与整段传递并存）
        if not stripped[at_niu_idx + len(_AT_NIU_PREFIX):].strip():
            logger.error(f"[AtPrefix] {_AT_NIU_PREFIX} 后无问题内容")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
            return (FORMAT_ERROR, None)
        # 超长检查（判定对象 = 完整 stripped——@niu-agent 进主 Agent 上下文需保护）：
        # 不截断，退回 FORMAT_ERROR 让子 Agent 精简后重新提问
        if len(question) > 8000:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"[输出超限额] 完整内容超过 8000 字符（当前 {len(question)} 字符），请精简后重新提问。如果无法精简，请用 @end 退出并说明原因。"})
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
            # T1（2026-08-15）：整段传递——question = 完整 stripped
            # （@user 前上下文 + @user + @ 后提问全传——用户看到整段）
            question = stripped
            # 空问题守卫：@user 标记后无内容仍 FORMAT_ERROR（裸 @user 即时纠错）
            if not stripped[after_marker:].strip():
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
                return (FORMAT_ERROR, None)
            # 无上限检查：@user 给用户看——用户无上下文限制——完整传
            # （IM 超长由适配层 _truncate_card_text 兜底——前端 tab 可滚动）
            return (INTERCEPTED_ASK_USER, question)
        # @user 后面紧跟非空白字符（如 @username），不拦截，继续走到格式错误

    # 格式错误
    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": _FORMAT_ERROR_PROMPT})
    return (FORMAT_ERROR, None)


def _check_main_agent_content_reply_to_suspended(stripped_content: str, messages: list) -> bool:
    """检测主 Agent content 是否在误回复同步挂起子 Agent（@ 任意位置，用户拍板标准会话惯用）。

    误回复模式：content 中任意位置出现 `@<子名>`（同步挂起子 Agent 名）→ 拦截。

    支持两种子名格式（兼容 LLM 复读历史 hex 后缀格式）：
    - 同步路径：browser-operator（方案 B 后默认格式）
    - 异步路径旧格式：browser-operator-708b（LLM 复读 000045 等历史日志格式时出现）

    命中时：append assistant content + user 错误提示，返回 True。
    未命中：返回 False（不拦截）。
    """
    if not stripped_content or "@" not in stripped_content:
        return False

    # 提取 content 中所有候选 @目标：旧逻辑（任意位置标点提取）+ 新正则全文（并集）
    candidates = []
    # (a) 旧逻辑：任意位置找 @，提取到中文/英文标点为止
    # （R5-A P3：跳过保留标记——防假设性同名子 Agent；中文标点紧跟格式
    #   "@browser-operator。我选择 2" 只有此逻辑能提取到，新正则要求 \s 分隔会漏）
    from agent.at_message_parser import _RESERVED_AT_TARGETS
    for m in re.finditer(r"@([A-Za-z0-9_\-]+)", stripped_content):
        t = m.group(1).rstrip(".,!?;:")
        if t in _RESERVED_AT_TARGETS:
            continue
        candidates.append(t)
    # (b) 新逻辑：@子Agent+空白/标点（排除保留标记）
    from agent.at_message_parser import _AT_PATTERN
    for m in _AT_PATTERN.finditer(stripped_content):
        candidates.append(m.group(1).rstrip(".,!?;:"))

    from agent.subagent_registry import SubagentRegistry
    for target_clean in dict.fromkeys(candidates):  # 去重保序
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
            continue  # 不在注册表，不拦截

        # 只拦截同步挂起 session（异步 running 走 db_monitor 原逻辑）
        if getattr(instance, "state", "running") != "waiting_for_answer":
            continue
        if not getattr(instance, "is_sync", True):
            continue

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
    return False


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
    try:
        return str(o)
    except Exception:
        # E4-15：坏 __str__（如 RecursionError）→ 安全占位文本，防序列化整轮失败
        return f"[无法序列化: {type(o).__name__}]"


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
    真删（removed > 0）后在 protect_end 处插入一条可见标记消息（user 角色），
    告知模型更早消息已被移除；返回值语义不变 = 删除条数，不含标记自身。
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
    if removed > 0:
        messages.insert(protect_end, {"role": "user", "content": f"[上下文提示：更早的 {removed} 条消息已因上下文超限被移除]"})
    return removed


_PLACEHOLDER_SUFFIX = "获取]"  # 裁剪族占位符后缀（带再生指引）
_LEGACY_PLACEHOLDER_SUFFIX = "输出已裁剪]"  # 旧后缀：兼容已含旧占位符的恢复会话
_FOLD_PLACEHOLDER_PREFIX = "[已折叠："  # 折叠族前缀：折叠占位符恒单行，未折叠渲染头行+换行+原文恒多行
_LEGACY_FOLD_ANCHORS = ("[输出#", "已折叠：")  # 旧带编号恢复会话文案双锚：[输出#N 已折叠：…（编号为纯诱饵已去）


def _is_tool_placeholder(content) -> bool:
    """判断 tool content 是否已是占位符。幂等依据。

    认三种形态（均单行）：
    - 裁剪族新：[{name} 输出已裁剪，如需原文可重新调用该工具获取] / [输出已裁剪，如需原文可重新调用对应工具获取]（后缀 "获取]"）
    - 裁剪族旧：[{name} 输出已裁剪] / [输出已裁剪]（后缀 "输出已裁剪]"，兼容已含旧占位符的恢复会话）
    - 折叠族（fold 工程 spec §4）：[已折叠：{工具名}({参数摘要≤80字符})，原占约 X%。]
      （pct None 变体省略占比分句；不带编号——编号对 LLM 零功能纯诱饵）。恢复会话兼容两代旧文案：
        ① 带编号版 [输出#N 已折叠：…]（去编号前）——双锚 "[输出#" 开头 + "已折叠：" 出现在前段（[:24]，
           覆盖任意位数 rowid；不用裸「已折叠 in content」以免误伤含该词的普通单行工具输出）；
        ② 最早版 [输出#N 已由 fold_tool_output 折叠：…如需原文请重新调用原工具获取]——以 "获取]" 收尾，
           由裁剪族后缀通道覆盖

    单行条件：所有占位符形态均为单行；T2 后窗口 tool 消息带头行恒为多行（头行+换行+原文），
    未折叠消息即使原文恰好以「获取]」/「输出已裁剪]」收尾也不会被误判为占位符而跳过应急裁剪；
    折叠族按前缀/双锚判定，单行条件已排除误判（未折叠渲染头行必含换行）。
    """
    if not isinstance(content, str):
        return False
    if "\n" in content:
        return False
    if content.startswith(_FOLD_PLACEHOLDER_PREFIX):
        return True
    # 旧带编号恢复会话文案：[输出#N 已折叠：…]（双锚——前缀 + 前段「已折叠：」）
    if (content.startswith(_LEGACY_FOLD_ANCHORS[0])
            and _LEGACY_FOLD_ANCHORS[1] in content[:24]):
        return True
    return content.startswith("[") and (
        content.endswith(_PLACEHOLDER_SUFFIX) or content.endswith(_LEGACY_PLACEHOLDER_SUFFIX)
    )


def _find_tool_name_from_assistant(messages: list, tool_idx: int, tool_call_id: str) -> str:
    """从当前 tool 消息向前找最近含 tool_calls 的 assistant，按 tool_call_id 匹配 function.name。
    ⚠ assistant.tool_calls 是 OpenAI 嵌套格式 {id, type, function:{name, arguments}}（L1004-1010），
    必须读 tc["function"]["name"]，不能读 tc["name"]（恒为 None）。
    """
    for j in range(tool_idx - 1, -1, -1):
        m = messages[j]
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []) or []:
            if not isinstance(tc, dict) or tc.get("id") != tool_call_id:
                continue
            fn = tc.get("function", {})
            if isinstance(fn, dict):
                return fn.get("name", "") or ""
            return ""
    return ""


def _placeholderize_tool_outputs(messages: list, target_tokens: int, protect_turns: int = 10) -> int:
    """阶段 1：把旧轮次 tool 输出替换为占位符，保留消息结构与 tool_call_id。

    从最早的 tool 消息开始逐个替换 content 为 "[{name} 输出已裁剪，如需原文可重新调用该工具获取]"（无 name 则 "[输出已裁剪，如需原文可重新调用对应工具获取]"），
    满足其一即停：
      a) count_messages_tokens(messages) <= target_tokens（达标即停，保留更多上下文）
      b) 到达保护边界：最近 protect_turns 轮对话（从尾部数 user 消息，尾部 user 算第 1 轮）内的 tool 不动
    已占位符化的消息跳过（幂等，用户约束：二次压缩不重复替换）。

    Args:
        messages: messages list（会被原地修改）
        target_tokens: 目标 token 数
        protect_turns: 保护最近 N 轮对话的 tool 输出（默认 10）
    返回替换条数。
    """
    if len(messages) <= 2:
        return 0
    # 保护边界：从尾部数 protect_turns 个 user 消息，protect_start 之前可替换
    protect_start = 0
    user_count = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            user_count += 1
            if user_count == protect_turns:
                protect_start = i
                break
    replaced = 0
    current_tokens = count_messages_tokens(messages)
    for i in range(len(messages)):
        if current_tokens <= target_tokens:
            break
        if i >= protect_start:
            break
        m = messages[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        if _is_tool_placeholder(content):
            continue  # 幂等：已占位符化，跳过
        name = m.get("name", "") or ""
        if not name:
            name = _find_tool_name_from_assistant(messages, i, m.get("tool_call_id", "") or "")
        m["content"] = f"[{name} 输出已裁剪，如需原文可重新调用该工具获取]" if name else "[输出已裁剪，如需原文可重新调用对应工具获取]"
        replaced += 1
        current_tokens = count_messages_tokens(messages)
    return replaced


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
    - 序列化链路任何异常（含 str() 降级抛 RecursionError）：返回错误 dict 兜底（E4-15）

    这样既保留 dict 语义（status 检查），又避免超大结果进 messages。
    """
    try:
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
    except Exception:
        # E4-15：外层兜底（非 BaseException——KeyboardInterrupt/CancelledError 保留穿透）
        return {"error": f"[工具结果序列化失败: {type(result).__name__}]"}


def _serialize_tool_result_data(data) -> str:
    """E4-15：工具结果序列化兜底（datastr 计算共用）。

    - dict/list：json.dumps(default=json_default)；异常 → 错误 dict 的 JSON 串
      （与统一关口 list 分支 except 语义一致——[工具结果序列化失败: <type>]）
    - 其他（含裸对象）：str()；异常 → 同错误文本（修复④——裸对象直调 str() 包 try/except）

    非 BaseException 语义：KeyboardInterrupt/CancelledError 保留穿透。
    """
    if type(data) in [dict, list]:
        try:
            return json.dumps(data, ensure_ascii=False, default=json_default)
        except Exception:
            return json.dumps({"error": f"[工具结果序列化失败: {type(data).__name__}]"}, ensure_ascii=False)
    try:
        return str(data)
    except Exception:
        return f"[工具结果序列化失败: {type(data).__name__}]"


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


def transform_history(messages: list[dict]) -> list[dict]:
    """history(dict 视图) → LLM 上下文消息变换（subagent_msg 跳过/空消息丢弃/
    孤儿 tool 校验跳过/valid_tcs 剥离悬空 tool_calls/_truncate_tool_content 30000 截断）。

    入口 agent_runner_loop 与工具轮重建（runner._on_tool_round_refresh）共用单一变换源——
    rebuild 必须与入口逐字节同制式（R3-A P1：悬空 tool_calls 注入会 OpenAI 400；
    不经截断会把已 cap 输出去截断回全量，缓存破口扩大+膨胀）。
    """
    # 从 assistant 消息的 tool_calls 构建 tool_call_id → tool_name 映射
    # 用于截断标记中显示工具名（DB 不存 tool_name，需从关联的 assistant 消息提取）
    _tc_id_to_name: dict[str, str] = {}
    # 收集所有有效的 tool_call_id（压缩可能留下孤立的 tool 消息）
    _valid_tc_ids: set[str] = set()
    for msg in messages:
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
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            _tool_response_ids.add(msg["tool_call_id"])

    result: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        # === 过滤 subagent_msg 消息，不塞进 LLM 上下文（@ 消息仅供前端展示） ===
        if role == "subagent_msg":
            continue
        content = msg.get("content", "")
        if role in ("user", "assistant") and (content or msg.get("tool_calls")):
            # plan 2026-09-10 D3 去展开：直传 str（图标记保留文本形态，发送层 sanitize 负责展开）
            entry = {"role": role, "content": content}
            # 还原 tool_calls（assistant 消息可能携带工具调用）
            if msg.get("tool_calls"):
                # 过滤掉没有对应 tool 响应的 tool_calls（压缩可能删除了 tool 输出）
                valid_tcs = [tc for tc in msg["tool_calls"] if tc.get("id") in _tool_response_ids]
                if valid_tcs:
                    entry["tool_calls"] = valid_tcs
                # 如果所有 tool_calls 都没有响应，不设置 tool_calls（变成纯文本消息）
            result.append(entry)
        elif role == "tool" and msg.get("tool_call_id") and content is not None:
            # 跳过孤立的 tool 消息（没有对应的 assistant tool_calls）
            if msg["tool_call_id"] not in _valid_tc_ids:
                logger.warning(f"[AgentLoop] Skipping orphan tool message: tool_call_id={msg['tool_call_id']}")
                continue
            # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
            # 截断超长的 tool 内容（DB 中保存了完整内容，但 LLM 上下文需要保护）
            tool_name = _tc_id_to_name.get(msg["tool_call_id"], "")
            # plan 2026-09-10 D3/D8：去展开（content 恒 str，图标记由发送层 sanitize 展开）；
            # 删 name 字段注入（规范 tool 消息键集仅 role/content/tool_call_id）
            entry = {"role": role, "content": _truncate_tool_content(content, tool_name), "tool_call_id": msg["tool_call_id"]}
            result.append(entry)
    return result


def _estimate_usage_ratio(messages) -> float | None:
    """发送前校准估算 usage（0-1）。估算失败返回 None（调用方按不达线处理）。"""
    try:
        from agent.context_assembler import calibration
        from agent.subagent import _read_context_window_tokens
        window = _read_context_window_tokens()
        if not window or window <= 0:
            return None
        est = calibration.estimate(count_messages_tokens(messages))
        return est / window
    except Exception:
        return None


# 压缩完成提示（spec §提示文案定稿——只此一句；落库 + 内存追加双通道：DB 落库序尾
# =压缩完成，重组内存追加供本轮可见）
_COMPRESSION_DONE_HINT = "[系统提示] 上下文压缩已完成。"


def _notify_compact_progress(status: str, mode: str = "auto") -> None:
    """压缩进度通知（R1-A P2-2）：同步函数内桥接 notify_compact_status_sync。"""
    try:
        from niu_api.chat import notify_compact_status_sync
        notify_compact_status_sync(status, mode=mode)
    except Exception:
        pass


def _compact_db_view(ctx) -> tuple[bool, dict]:
    """从 DB 全量消息机械压实（spec Task 5）。返回 (是否压实成功, stats)。

    ctx = 压缩上下文鸭子对象（R1-B P1-2），须含 _sync_get_messages。
    """
    try:
        from agent.context_assembler import compaction
        db_messages = ctx._sync_get_messages()
        if not db_messages:
            return False, {}
        system_msg = getattr(ctx, "_system_msg", None)
        new_view, stats = compaction.build_compact_view(
            db_messages, system_msg=system_msg,
            blocks_db_path=getattr(ctx, "_blocks_db_path", None),
        )
        ctx._last_compacted_view = new_view  # 供重组消费
        return True, stats
    except Exception as e:
        logger.exception(f"[Compression] DB compact failed: {e}")
        return False, {}


def _rebuild_messages_after_compact(orig_messages, ctx, summary_text: str) -> list:
    """重组待发 messages：整体替换 + 待执行单元置最后 + 尾引导补（R4-A P0-1 修订）。

    核心事实（R3-B + 代码实证）：
    - compaction 只写指针块**绝不删 DB 行**；agent_loop 工具轮 persist 后经
      _on_tool_round_refresh 从 DB 重建 messages——messages 与 DB 几乎同步。
    - 压缩后**整体替换**为压实视图（[system]+[索引]+[窗口占位符化]）即完整正确
      上下文，不 re-append unit 原文（防反胀/重复/孤儿 tool）。

    **用户拍板不变式：原指令（待执行内容）必须是最后一条**（spec §受控压缩流程 e）。
    压实视图窗口（keep≥1）恒含当前待执行单元原文——因此：
    1. 从视图剥离**最后单元**（其 user 起点 → 尾：与 orig_messages 尾部对齐识别；
       首轮=当前 user 指令；轮间=当前 continuation 单元）
    2. 视图主体 = [system]+[索引]+[剥离后的窗口]
    3. 追加三件套（内存构造，非 DB 重读——与 T2 落库共用 _triplet_messages，
       两侧同构）：user _SUMMARY_PROMPT → assistant summary_text（空则跳）→
       user 完成提示。总结仅由本通道 + DB 落库通道各供一份（恒单份）
    4. 未落库尾引导（supplement/next_prompt，DB 无）——引导实际是轮末 append 在
       消息流中、语义上应先于待执行内容，故放三件套后、待执行单元前

    顺序：[视图主体] + [三件套] + [未落库尾引导] + [待执行单元(最后)]
    """
    system = orig_messages[0] if orig_messages and orig_messages[0].get("role") == "system" else None
    compacted = ctx._last_compacted_view or []
    # 压实视图窗口部分（去 system）
    window = [m for m in compacted if m.get("role") != "system"]
    # R6-A P0-1 / B-P1-2 修正：待执行单元识别——orig_messages 尾部可能是不落库的
    # 引导 user（supplement/next_prompt，DB 无）。真正的待执行单元起点 = orig 中
    # **content 也出现在窗口（=已 persist 落库）的最后一个 user**；其后的尾部连续
    # user 若不在窗口（未落库引导）则收进 tail_guides 放完成提示后、待执行单元前。
    # 识别：从 orig 尾向前找第一个"content 在窗口 user 集合中"的 user = 待执行起点。
    window_user_contents = {
        m.get("content") for m in window if m.get("role") == "user"
    }
    # 防御声明（spec 3.6）：三件套 user 行 content（_SUMMARY_PROMPT/_COMPRESSION_DONE_HINT
    # 常量）永不得进本集合——剥离只从 _last_compacted_view 切（本就无三件套），
    # Approach A 结构性保证；三件套在剥离**之后** append → 不可能被误当待执行单元。
    pending_start = -1
    for i in range(len(orig_messages) - 1, -1, -1):
        m = orig_messages[i]
        if m.get("role") == "user" and (m.get("content") or "") in window_user_contents:
            pending_start = i
            break
    # 无匹配（orig 全部未落库——异常）→ 兜底：窗口最后 user 起点
    if pending_start < 0:
        for i in range(len(orig_messages) - 1, -1, -1):
            if orig_messages[i].get("role") == "user":
                pending_start = i
                break
    pending_unit = orig_messages[pending_start:] if pending_start >= 0 else []
    # 从视图剥离待执行单元：窗口尾部从"与 pending_unit 起点同 content 的 user"起剥
    stripped: list = []
    if pending_unit:
        pu_first_content = pending_unit[0].get("content") or ""
        cut = None
        for i in range(len(window) - 1, -1, -1):
            if window[i].get("role") == "user" and (window[i].get("content") or "") == pu_first_content:
                cut = i
                break
        if cut is not None:
            stripped = window[cut:]
            window = window[:cut]
    new_msgs: list = []
    if system is not None:
        new_msgs.append(system)
    new_msgs.extend(window)
    # 三件套（Approach A——内存构造，非 DB 重读；与 T2 落库共用 _triplet_messages，
    # 两侧同构）：user _SUMMARY_PROMPT → assistant summary_text（空则跳）→ user
    # 完成提示。总结仅由本通道 + DB 落库通道各供一份（恒单份）。旧"从 stripped 抽
    # 总结防双份"逻辑已删（spec 3.2 P2-1：生产路径总结恒在压实后落库，
    # _last_compacted_view 永不含本轮总结；剥离段只从无三件套的视图切 →
    # 结构性不会误剥/双份）。
    new_msgs.extend(_triplet_messages(summary_text))
    # 未落库尾引导（R6 修订）：orig 中 content **不在窗口**的尾部连续 user
    # （supplement/next_prompt/动态块，DB 无）——它们可能在待执行起点之后
    # （消息流最后 append）。反向收集：遇到 content 在窗口（已 persist）的 user
    # 停止；跳过动态块前缀（下轮 on_before_llm 幂等重插）。
    tail_guides: list = []
    for m in reversed(orig_messages):
        if m.get("role") != "user":
            if tail_guides:
                break  # 已收集引导后遇非 user → 停止（引导是连续尾部）
            continue  # 未开始收集时跳过非 user（窗口剥离段在尾）
        content = m.get("content") or ""
        if content.startswith("[系统动态信息]"):
            if tail_guides:
                break
            continue  # 动态块不收（未开始收集时跳过）
        if content in window_user_contents:
            break  # 已 persist（视图有同文本 user）→ 待执行起点，停止
        tail_guides.append(m)
    tail_guides.reverse()
    new_msgs.extend(tail_guides)
    # 待执行单元最后（用户拍板不变式：原指令最后一条）
    if stripped:
        new_msgs.extend(stripped)
    elif pending_unit:
        new_msgs.extend(pending_unit)  # 视图无（异常）→ 用 orig 的
    return new_msgs


def _gate_release() -> None:
    """AUTO_GATE 统一 release（幂等，防闩锁永久化——R2-A P1-1：任何出口都解闩）。

    注（R6-A P2-1 + FinalReview B-P3-1）：**失败/早退**路径经条件包装
    （run_controlled_compression._release_if_acquired / 门 _gate_release_if_acquired）
    仅当本轮确实 try_acquire 过才调本函数——无条件调用会误清他轮成功压实保留的
    滞回闩锁 → 下一轮冗余 auto 重压。**成功压实**路径不调本函数，改按压后估算
    ratio 是否回落到复位线决定 release（见 run_controlled_compression 成功分支）
    ——保住滞回语义：压实后仍超线则保持闩锁，本 loop 内不再重复触发分钟级压缩
    （防长任务每轮停顿 + DB 冗余总结累积）。
    """
    try:
        from agent.context_assembler import compaction
        compaction.AUTO_GATE.release()
    except Exception:
        pass


def _clear_compression_intent_on_abnormal_exit(handler) -> None:
    """异常退出清理（FinalReview B-P2-1）：清未消费压缩意图，防泄漏到下一会话首轮门。

    忙时 /compact 置 manual 意图后立即返回；若正在跑的 run 在再次到达发送前门之前
    异常退出（首轮 stop 检查、LLM error、溢出），残留意图会被下一会话首轮门消费 →
    意外压缩。正常出口（CURRENT_TASK_DONE/MAX_TURNS）不清——用户按了 /compact 就该压，
    顺延到下一条消息首轮门 = 设计内"任务间隙执行"语义。子 Agent 不碰全局意图
    （防误清主 Agent 在途 manual——子 Agent loop 嵌在主 Agent 工具轮内运行）。
    """
    if getattr(handler, "_is_subagent", False):
        return
    reset_compression_intent()


def run_controlled_compression(messages, ctx, client, turn, release_on_failure: bool = True) -> tuple[list, bool]:
    """发送前受控压缩（spec §受控压缩流程）——完整体（ctx 鸭子对象，R1-B P1-2）。

    调用链（回调契约）：agent_runner_loop 门内只调 on_compression_request(messages, turn)
    回调（runner 提供）；runner 的 _on_compression_request 构造 ctx 后调本函数——
    门不经手 ctx。执行顺序：提炼前置 → 模型承上启下总结（仅内存，不落库）
    → 机械压实（DB 全量 build_compact_view）→ 压实成功后落库三件套
    （bypass+skip_mirror，_persist_compression_triplet）→ 消息重组（单元感知：
    待执行单元最后 + 三件套内存追加）。各步进度通知经 notify_compact_status_sync
    （本函数同步不能 yield）。

    闩锁语义（R6-A P2-1 滞回 + FinalReview B-P3-1 条件化）：失败/早退出口按
    release_on_failure 条件解闩——仅当调用方本轮确实 try_acquire 过才 release
    （门传 gate_acquired；闲时直调自己置闩传 True）。无条件 release 会误清他轮
    成功压实保留的滞回闩锁 → 下一轮冗余 auto 重压。成功压实仅按压后 usage 回落
    < 复位线才 release——未回落保持闩锁，本 loop 内不再重复触发分钟级压缩。

    Args:
        messages: 当前待发消息列表（会被重组）
        ctx: 压缩上下文鸭子对象（R1-B P1-2 修订，非 NiuHandler）——须含
            _sync_get_messages() / _sync_add_message(...) / _llm_config / _store
        client: LLM client（总结调用用）
        turn: 当前轮号（进度日志/冷却）
        release_on_failure: 失败/早退时是否解闩（B-P3-1：门传本轮 gate_acquired；
            闲时直调自己 try_acquire 过 → True）。默认 True 兼容既有直接调用方。

    Returns:
        (重组后 messages, 是否执行了压缩)。失败 → 返回原样 (messages, False)，
        由调用方决定是否继续发送——绝不静默丢消息。
    """
    logger.info(f"[Compression] run_controlled_compression invoked at turn {turn}")

    def _release_if_acquired() -> None:
        # B-P3-1：仅当调用方本轮确实 try_acquire 过才解闩（release_on_failure 传入）——
        # 无条件 release 会误清他轮成功压实保留的滞回闩锁 → 下一轮冗余 auto 重压
        if release_on_failure:
            _gate_release()

    # 步骤 1：提炼前置（硬约束——提炼未完不压实）
    if _extract_cooldown_active():
        logger.warning(f"[Compression] 提炼失败冷却期，跳过本轮压缩")
        _release_if_acquired()
        return messages, False
    _notify_compact_progress("started", mode="auto")
    extracted_ok = _extract_f1_before_compress(messages, ctx)
    if not extracted_ok:
        logger.warning("[Compression] F1 提炼未完成，本轮跳过压缩（防未提炼内容出窗丢失）")
        _mark_extract_failed()
        _release_if_acquired()
        _notify_compact_progress("done", mode="auto")  # 终态必推，防前端圆环卡死
        return messages, False
    # 步骤 2：模型承上启下总结（仅内存——三件套落库统一在压实成功后，spec 3.2）
    summary_text = ""
    try:
        _notify_compact_progress("started", mode="summary")
        summary_text = _run_summary_llm(messages, client)
    except Exception as e:
        logger.warning(f"[Compression] 总结步失败（跳过总结仍压实）: {e}")
    # 步骤 3：机械压实（DB 全量）
    compacted = False
    _stats = {}
    try:
        from agent.context_assembler import compaction
        compacted, _stats = _compact_db_view(ctx)
    except Exception as e:
        logger.warning(f"[Compression] 压实异常: {e}")
    if not compacted:
        logger.warning("[Compression] DB 压实失败，返回原消息不重组")
        _release_if_acquired()  # B-P3-1：条件解闩（仅本轮 acquire 过）；成功路径不解
        _notify_compact_progress("done", mode="auto")
        return messages, False
    # 三件套落库（spec 3.2 步骤 3——压实成功后才执行 → 落在保留窗口尾，不被本次
    # 压实归档；压实失败已早退 → 不落，防"宣称完成但未压缩"）
    _persist_compression_triplet(ctx, summary_text)
    # R6-A P2-1 / R7-A P1 滞回（修正）：压实成功——门在 gate_hit 时已统一
    # AUTO_GATE.try_acquire 闩锁（含 manual，见 agent_runner_loop 门）。压后估算回落
    # < 复位线才 release（供下轮重检）；仍 ≥ 触发线 → 保持闩锁，本 loop 内不再
    # 重复触发分钟级压缩（防长任务每轮停顿 + DB 冗余总结累积）。manual 同理：
    # 用户刚手压完若仍未回落，不立即 auto 重压（R7-A 焦点 3）。
    try:
        from agent.context_assembler import compaction
        _post_ratio = None
        if _stats.get("usage") is not None:
            _post_ratio = float(_stats.get("usage"))
        if _post_ratio is not None and _post_ratio < compaction.reset_ratio():
            compaction.AUTO_GATE.release()  # 已回落 → 解除闩锁（下轮可再触发）
        # else：未回落 → 保持闩锁（本函数不 release，天然滞回）
    except Exception:
        pass  # release 失败无害（/new 兜底复位）
    # 步骤 4：重组 messages（单元感知）。P1-3：压实+落库已成功 → did=True 固定，
    # 重组异常不翻转——只降级为"压实视图 + 手工完成提示"，绝不返回"未压缩"→
    # 防门误判 manual 重设 → 下轮重复压缩双份三件套。
    try:
        new_msgs = _rebuild_messages_after_compact(messages, ctx, summary_text)
    except Exception as e:
        logger.exception(f"[Compression] 重组失败，降级为压实视图（保留 system 行）+ 完成提示: {e}")
        # 保留 system 行在 [0]：门后重跑 on_before_llm 依赖 messages[0].role == "system"
        # （否则 _assemble_system_message 早退返回 "" → 该次及同 run 后续轮全无 system）。
        # on_before_llm 只原地刷新该行 content、从不插新 system 行 → 无双 system 风险。
        new_msgs = list(ctx._last_compacted_view or [])
        # 手工把完成提示插到最后一个 user 之前——防破"原指令最后"不变式
        _insert_at = None
        for i in range(len(new_msgs) - 1, -1, -1):
            if new_msgs[i].get("role") == "user":
                _insert_at = i
                break
        if _insert_at is not None:
            new_msgs.insert(_insert_at, {"role": "user", "content": _COMPRESSION_DONE_HINT})
        else:
            new_msgs.append({"role": "user", "content": _COMPRESSION_DONE_HINT})
    # done 通知两路径必推（成功与降级）——防前端圆环卡死
    _notify_compact_progress("done", mode="auto")
    return new_msgs, True


# 压缩前总结 prompt（spec §提示文案定稿——承上启下，非历史抢救）
_SUMMARY_PROMPT = (
    "[系统提示] 上下文即将压缩，超出保留范围的早期对话将被归档移出。\n"
    "压缩前，请你对当前工作状态做一次承上启下的总结，让压缩后的对话\n"
    "能无缝衔接、立即继续，不被中断。总结须包含：\n"
    "1. 当前未完成的工作：手上正在进行、尚未结束的任务，逐项列出，\n"
    "   说明做到哪一步、下一步要做什么\n"
    "2. 用户近期的特殊要求或特别提示：你还没做完的、或后续需要一直\n"
    "   注意的（用户偏好、约束、待办），逐条重复\n"
    "3. 本阶段主要完成的工作：简要归纳最近这段对话做了什么、结论是什么\n"
    "请直接输出总结内容，不要调用工具。"
)


def _run_summary_llm(messages, client) -> str:
    """调 LLM 生成承上启下总结（tools=[]）。失败/stream_error → ""。"""
    try:
        summary_messages = list(messages) + [{"role": "user", "content": _SUMMARY_PROMPT}]
        gen = client.chat(messages=summary_messages, tools=[])
        resp = exhaust(gen)
        if resp is None:
            return ""
        if getattr(resp, "stream_error", False):
            logger.warning(f"[Compression] 总结 LLM error, skip: {resp.error_msg}")
            return ""
        return getattr(resp, "content", "") or ""
    except Exception as e:
        logger.error(f"[Compression] 总结调用失败: {e}")
        return ""


def _triplet_messages(summary_text: str) -> list[dict]:
    """压缩产物三件套消息构造（T2 落库与 T3 内存追加共用——两侧同构，spec 3.6）。

    序 = [user _SUMMARY_PROMPT][assistant summary_text（空则跳）][user _COMPRESSION_DONE_HINT]。
    空总结只两条——防空 content="" assistant 行进 client.chat 致 provider 400
    （总结失败降级场景，spec 3.4）。
    """
    msgs: list[dict] = [{"role": "user", "content": _SUMMARY_PROMPT}]
    if summary_text:
        msgs.append({"role": "assistant", "content": summary_text})
    msgs.append({"role": "user", "content": _COMPRESSION_DONE_HINT})
    return msgs


def _persist_compression_triplet(ctx, summary_text: str) -> None:
    """三件套落库（spec 3.2 步骤 3——压实成功后调用）：全行 skip_mirror + bypass_at_extract。

    各行独立：单行返回 None（写失败）→ warning 累积，不阻断后续行；完成行（最后
    一条）失败提级显著 warning（P2-3：本轮可见性由重组无条件内存追加天然满足，
    不做条件化——DB 缺完成行仅影响下轮组装，罕见 DB 故障可接受）。
    """
    _rows = _triplet_messages(summary_text)
    for i, m in enumerate(_rows):
        try:
            _res = ctx._sync_add_message(
                role=m["role"], content=m["content"],
                skip_mirror=True, bypass_at_extract=True,
            )
        except Exception as e:
            logger.warning(f"[Compression] 三件套落库第 {i + 1} 行（{m['role']}）异常: {e}")
            continue
        if _res is None:
            if i == len(_rows) - 1:
                logger.warning(
                    "[Compression] 三件套完成行（压缩完成提示）DB 写失败——本轮内存视图仍含"
                    "完成提示，但下轮 DB 组装将缺该行（罕见 DB 故障，可接受）"
                )
            else:
                logger.warning(f"[Compression] 三件套落库第 {i + 1} 行（{m['role']}）返回 None（写失败），继续后续行")


def _persist_summary_without_extract(ctx, summary_text: str) -> str | None:
    """总结 assistant 行落库的薄包装（T2 后生产调用点已移至 _persist_compression_triplet）。

    bypass @ 提取 + skip_mirror（spec §消息形态 风险 C）。保留本函数为既有
    import/断言引用点存活（P1-1 最小破坏）：只落 assistant 行（summary 非空时）。
    ctx = 压缩上下文鸭子对象（R1-B P1-2），须含 _sync_add_message。
    空文本早退不落库——空 assistant 行会污染历史视图。
    """
    if not summary_text:
        return None
    try:
        return ctx._sync_add_message(
            role="assistant", content=summary_text,
            skip_mirror=True, bypass_at_extract=True,
        )
    except Exception as e:
        logger.warning(f"[Compression] 总结落库失败: {e}")
        return None


def _f1_has_arrears(f1_path: str | None = None) -> bool:
    """F1 是否有未提炼内容（非空且首行是记录块）。"""
    import os
    from agent.md_mirror import F1_PATH
    p = f1_path or F1_PATH
    try:
        return os.path.exists(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def _align_f1_sync(store, f1_path: str | None = None) -> None:
    """executor 线程桥接主事件循环跑 align_f1_with_store（best-effort）。"""
    try:
        import asyncio
        from niu_api.chat import _main_loop
        from niu_api.md_alignment import align_f1_with_store
        from agent.md_mirror import F1_PATH
        loop = _main_loop
        if loop is None or not loop.is_running():
            return
        fut = asyncio.run_coroutine_threadsafe(
            align_f1_with_store(store, f1_path or F1_PATH), loop)
        fut.result(timeout=30)
    except Exception as e:
        logger.warning(f"[Compression] align_f1 skipped: {e}")


def _get_message_store_sync():
    """executor 线程桥接主循环取 MessageStore（R4-A P2-3：ctx._store 恒 None 时
    align 用）。失败返回 None（align best-effort 跳过）。"""
    try:
        import asyncio
        from niu_api.chat import _main_loop
        from agent.session import get_message_store
        loop = _main_loop
        if loop is None or not loop.is_running():
            return None
        fut = asyncio.run_coroutine_threadsafe(get_message_store(), loop)
        return fut.result(timeout=10)
    except Exception:
        return None


def _call_extractor_sync(llm_config, f1_path=None) -> str:
    """同步调 entity-extractor 提炼 F1（复睡眠管道 _call_entity_extractor_on_f1）。"""
    try:
        from niu_api.compat import _call_entity_extractor_on_f1
        return _call_entity_extractor_on_f1(llm_config, f1_path)
    except Exception as e:
        return f"[提炼调用异常: {e}]"


def _extractor_guards_pass(result: str) -> bool:
    """三守卫：overflow/incomplete/failure 任一 → False（不剪 F1）。"""
    try:
        from niu_api.compat import (
            _is_subagent_overflow, _is_subagent_incomplete, _is_subagent_failure,
        )
        return not (
            _is_subagent_overflow(result)
            or _is_subagent_incomplete(result)
            or _is_subagent_failure(result)
        )
    except Exception:
        return False


def _relay_cut_f1(result: str, f1_path=None) -> None:
    """解析 processed_line 并剪 F1（复用 compat._parse_and_relay_f1）。"""
    try:
        from niu_api.compat import _parse_and_relay_f1
        _parse_and_relay_f1(result, f1_path)
    except Exception as e:
        logger.warning(f"[Compression] relay cut F1 failed: {e}")


def _extract_f1_before_compress(messages, ctx) -> bool:
    """提炼前置（spec §受控压缩流程 a）：F1 欠账提炼入库完成才放行压实。

    executor 线程内同步调用。ctx = 压缩上下文鸭子对象（含 _store/_llm_config）。
    流程：align F1↔DB → F1 有欠账则调 entity-extractor 提炼（同步阻塞至 @end）
    → 三守卫通过才剪 F1。

    Returns:
        True = F1 无欠账或提炼完成（可压实）；False = 提炼失败/未完成（调用方
        冷却退避不压实——提炼未完绝不压实，防未提炼内容出窗丢失）。
    """
    try:
        from agent.md_mirror import F1_PATH
        f1_path = getattr(ctx, "_f1_path", None) or F1_PATH
        # align（best-effort，F1 与 DB 对齐防剪错前缀）——R4-A P2-3 修订：
        # ctx._store 恒 None（runner 无 _store 属性）时经主循环 get_message_store 取，
        # 否则生产路径 align 从不执行 → /clear 后 F1/DB 漂移会剪错前缀（本工程要防的失效）
        store = getattr(ctx, "_store", None)
        if store is None:
            try:
                store = _get_message_store_sync()
            except Exception as e:
                logger.warning(f"[Compression] align store unavailable: {e}")
                store = None
        if store is not None:
            _align_f1_sync(store, f1_path)
        if not _f1_has_arrears(f1_path):
            return True
        llm_config = getattr(ctx, "_llm_config", None)
        if llm_config is None:
            logger.warning("[Compression] 无 llm_config，跳过提炼前置（按无欠账放行）")
            return True
        result = _call_extractor_sync(llm_config, f1_path)
        logger.info(f"[Compression] entity-extractor result: {str(result)[:200]}")
        if not _extractor_guards_pass(result):
            logger.warning("[Compression] 提炼未正常完成 — F1 不剪切，不压实（残留睡眠管道补提炼）")
            return False
        _relay_cut_f1(result, f1_path)
        return True
    except Exception as e:
        logger.exception(f"[Compression] 提炼前置异常: {e}")
        return False


def compaction_trigger_ratio_local() -> float:
    """压实触发线（发送前达线判定用）。惰性导入防环。"""
    try:
        from agent.context_assembler.compaction import trigger_ratio
        return trigger_ratio()
    except Exception:
        return 0.80


def _gate_release_if_acquired(acquired: bool) -> None:
    """本轮门 acquire 过闩锁才 release（幂等；未 acquire 不误释放他人闩锁）。"""
    if acquired:
        try:
            from agent.context_assembler import compaction
            compaction.AUTO_GATE.release()
        except Exception:
            pass


# 提炼失败冷却（R1/R3 修订）：提炼失败后冷却期内不重试提炼——防长任务每次发送前
# 都重试分钟级 entity-extractor（停顿风暴）。残留 F1 由睡眠管道补提炼。
# R3-A P2-1：用 monotonic 时间而非 turn 计数——turn 每次 agent_runner_loop 归零，
# turn 键会让失败冷却跨会话泄漏（会话B 前 N 轮全跳过压缩）；时间键随真实流逝冷却。
_EXTRACT_RETRY_COOLDOWN_SECONDS = 600.0  # 10 分钟冷却（替代轮数制）
import time as _time
_extract_failed_at_ts: float = -10**18  # 模块级：上次提炼失败 monotonic 时间戳


def _extract_cooldown_active() -> bool:
    """提炼失败冷却是否生效（距上次失败 < 冷却秒数）。覆盖 Task 2 桩。"""
    return (_time.monotonic() - _extract_failed_at_ts) < _EXTRACT_RETRY_COOLDOWN_SECONDS


def _mark_extract_failed() -> None:
    global _extract_failed_at_ts
    _extract_failed_at_ts = _time.monotonic()


def _reset_extract_cooldown() -> None:
    """/new 复位提炼失败冷却（R3-A P2-1）：置时间戳为远过去，冷却立即失效。"""
    global _extract_failed_at_ts
    _extract_failed_at_ts = -10**18


def agent_runner_loop(
    client,
    system_prompt: str = "",  # 向后兼容（system_message 优先）
    user_input=None,
    handler=None,
    tools_schema=None,
    max_turns: int | None = 40,  # None = 无上限（子 Agent 长程任务跑到底）；主 Agent 默认 40 轮
    verbose=True,
    initial_user_content=None,
    history=None,  # Optional: list of {"role": "user/assistant", "content": str}
    on_turn_end=None,  # Optional: callback(messages, tools_schema, turn) -> tools_schema
    context_window_tokens=0,  # 0 means no limit check (backward compatible)
    context_fifo_threshold=0,  # 0 means no FIFO truncation; >0 means max token budget for sub-agents
    context_target_threshold=0,  # FIFO 裁剪目标 token 量
    on_tool_round_refresh=None,  # 每工具轮 persist 后视图重建回调（2026-09-02）：主 Agent 传入，原地 messages[:] 替换；None=子 Agent 跳过
    enable_supplement=True,  # False for sub-agents to prevent stealing main agent's supplements
    system_message: dict | None = None,  # 已组装好的 system message（首轮即带 cache_control）
    supplement_drain=None,  # 子 Agent 传入自己的 drain 函数；None 时走全局 drain_supplement
    memory_context: Any | None = None,  # 阶段二新增：异步子 Agent 进度数据，None=主 Agent 路径不更新
    resumed_messages=None,  # 阶段四新增：挂起恢复路径，传入则跳过 messages 构造直接用
    on_before_llm=None,  # Optional: callback(messages, turn) called before each LLM call; modifies messages[0] in place
    stop_predicate: Callable | None = None,  # 停止穿透：停止判定谓词（默认 None = 全局 is_stop_requested；子 Agent 由 call_subagent 传入）
    on_compression_request=None,  # 统一压缩入口（spec 2026-09-06）：主 Agent runner 传回调执行受控压缩；None=子 Agent 跳过（走保留的响应后 FIFO/占位符化 else 分支）
):
    from agent.runner import clear_stop, drain_supplement, is_stop_requested
    from agent.generic.interruptible import run_interruptibly
    stop_predicate = stop_predicate or is_stop_requested  # 默认全局停止检查

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
        # 变换逻辑抽为模块级纯函数 transform_history（工具轮重建共用单一变换源——
        # rebuild 与入口逐字节同制式，R3-A P1）；守卫与 resumed_messages else 分支结构不变
        if history:
            messages.extend(transform_history(history))

        # Add current user message（直传 str；resumed 分支不走此处）
        _current_user_content = initial_user_content if initial_user_content is not None else user_input
        messages.append({
            "role": "user",
            "content": _current_user_content,
        })

    # Debug info only - logging is done in ToolClient.chat where the real prompt is built
    logger.info(f"[Debug] agent_runner_loop: {len(messages)} messages (history: {len(history) if history else 0})")

    turn = 0
    last_prompt_tokens = 0
    handler._last_prompt_tokens = 0
    handler._last_cached_tokens = None
    handler._done_hooks = []
    handler.max_turns = max_turns
    # V4: 通知前端进入忙碌状态
    yield StreamEvent("system", "chat_busy")

    _harness_fail_count = 0
    _max_harness_retries = 3
    _truncation_retry_count = 0
    _max_truncation_retries = 3
    _parse_fail_count = 0  # E4-01：同一轮内连续参数解析失败计数（每轮解析循环起点重置 + 解析成功清零——触发严格限定"同一轮连续 3 次"）
    _max_parse_failures = 3
    _sync_suspend_warned = False  # 同步子 Agent 挂起警告：每次 agent_runner_loop 调用重置，最多注入一次（2026-08-31 用户拍板）
    warning_threshold = _read_warning_threshold()

    while handler.max_turns is None or turn < handler.max_turns:
        turn += 1
        # --- Stop flag check ---
        if stop_predicate():
            logger.info("[AgentLoop] Stop requested, exiting loop")
            if not getattr(handler, "_is_subagent", False):
                clear_stop()  # 主 Agent 自己消费停止意图
            # 子 Agent（_is_subagent=True）不清全局标志——被主 Agent 停止意图打断时保留给主 Agent 消费
            _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}
        # === 上下文使用率检测（prompt_tokens 驱动）===
        # 主 Agent（_is_subagent=False）响应后不再压实——压缩已迁发送前门
        # （spec 2026-09-06 统一压缩入口）；子 Agent 保留原处 FIFO/占位符化
        # else 分支（唯一主动裁剪保护，R1 定案：不迁移防双裁剪）。
        if last_prompt_tokens > 0 and context_window_tokens > 0:
            usage_ratio = last_prompt_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                if getattr(handler, "_is_subagent", False):
                    # 子 Agent：阶段 1 tool 占位符化 → 仍超才阶段 2 FIFO 兜底
                    target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.50)
                    replaced = _placeholderize_tool_outputs(messages, target_tokens)
                    if count_messages_tokens(messages) > target_tokens:
                        removed = _fifo_prune(messages, target_tokens, is_resumed=(resumed_messages is not None))
                        if removed > 0:
                            logger.info(f"[FIFO] Proactive pruning: {last_prompt_tokens}/{context_window_tokens} tokens "
                                        f"({usage_ratio:.1%} > {warning_threshold:.0%}), removed {removed} messages, "
                                        f"now ~{count_messages_tokens(messages)} tokens (target {target_tokens})")
                    elif replaced > 0:
                        logger.info(f"[ToolCrop] placeholderized {replaced} tool outputs, "
                                    f"now ~{count_messages_tokens(messages)} tokens (target {target_tokens})")
            # 旧 FIFO 回退：只在首轮（last_prompt_tokens==0）时执行
        if context_fifo_threshold > 0 and len(messages) > 2 and last_prompt_tokens == 0:
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
        # 停止检查：动态注入（on_before_llm，含 LightRAG 检索）放弃后立即退出，
        # 不发起 LLM 调用（注入可中断化 Task 2 的配套——放弃注入后主 Agent 立即 STOPPED）
        if stop_predicate():
            logger.info("[AgentLoop] Stop requested before LLM call, exiting")
            if not getattr(handler, "_is_subagent", False):
                clear_stop()  # 主 Agent 自己消费停止意图
            _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}
        # === 统一压缩门（spec 2026-09-06）：发送前检查压缩意图/达线 ===
        # 主 Agent（on_compression_request 传入）才走受控压缩；子 Agent（None）
        # 保持响应后 FIFO/占位符化 else 分支（既有保护不删，R1 定案）。
        if on_compression_request is not None:
            had_intent, _reason = consume_compression()
            ratio = _estimate_usage_ratio(messages)
            # R7-A P1 滞回统一：达线 auto 先 AUTO_GATE.try_acquire(ratio) 置闩锁——
            # run_controlled_compression 成功按压后回落决定是否 release（保持闩锁 =
            # 本 loop 不再重压）；失败早退由 _gate_release 解闩。
            # manual 意图**不看闩锁**（R8-A P3-1：用户显式意图优先，闩锁期照常执行是
            # 正确行为——勿因下方注释误解而"修"成被挡）；manual 低水位（ratio<触发线）
            # 时试闩实传 ratio 原值（非 clamp——ratio 为 None 才回落触发线，R13 修正）。
            gate_acquired = False
            try:
                from agent.context_assembler import compaction
                if ratio is not None:
                    gate_acquired = compaction.AUTO_GATE.try_acquire(ratio)
            except Exception:
                gate_acquired = ratio is not None and ratio >= compaction_trigger_ratio_local()
            # 达线判定（R8-A P1 修正：ratio_hit 必须 gate_acquired——try_acquire True ⇔
            # 达线且未闩；否则已闩未回落期 ratio 仍达线 → 每 send 重跑分钟级压缩
            # （R7-A P1 原样保留的缺陷）。manual 意图不看闩锁（用户显式优先，P3-1 注
            # 释修正：闩锁期 manual 照常执行是正确行为，勿"修"）
            intent_hit = had_intent and (_reason != "auto" or gate_acquired)
            ratio_hit = (not had_intent) and gate_acquired
            gate_hit = intent_hit or ratio_hit
            # auto 意图复检（R1-B P2-2）：消费时 usage 已回落 < 触发线 → 放弃
            if had_intent and _reason == "auto" and ratio is not None \
                    and ratio < compaction_trigger_ratio_local():
                gate_hit = False
            if gate_hit and not gate_acquired and had_intent and _reason == "manual":
                # manual 意图 ratio 未达线（用户手压但上下文未达线）——try_acquire 未闩。
                # R12-A P3-1 修正：实传 ratio 原值试闩（非 clamp——ratio 0.3 永不闩、
                # try_acquire 返回 False、manual 仍照压，成功后 release 幂等无害）。
                # 注释与码一致：manual 不依赖闩锁语义，试闩仅为防 auto 已闩时语义混乱。
                try:
                    from agent.context_assembler import compaction
                    gate_acquired = compaction.AUTO_GATE.try_acquire(
                        ratio if ratio is not None else compaction.trigger_ratio())
                except Exception:
                    gate_acquired = True
            if gate_hit:
                # R4-A P1-1 + R5-A P0-1 + R7 修订：提炼冷却期（manual 或 auto）——
                # 不执行压缩，落回正常发送；**绝不 continue**（空转杀长任务）。
                # R9-A P3-1：auto 冷却也显式 defer 提示，不 yield"正在整理"误导。
                if _extract_cooldown_active():
                    if had_intent and _reason == "manual":
                        logger.warning("[Compression] manual intent during extract cooldown, deferring")
                        request_compression("manual")  # 重新置位（下轮冷却过再执行）
                    else:
                        logger.warning("[Compression] auto compaction during extract cooldown, deferred")
                    yield StreamEvent("system", "内容提炼冷却中，压缩将在可执行时自动进行…")
                    gate_hit = False  # 落回正常发送
                    _gate_release_if_acquired(gate_acquired)  # 释放本轮闩锁
                if gate_hit:
                    logger.info(f"[Compression] Pre-send gate: intent={had_intent}({_reason}) ratio={ratio}")
                    yield StreamEvent("system", "正在整理上下文，请稍候…")
                    try:
                        # B-P3-1：传本轮 gate_acquired——回调链透传给 run_controlled_compression
                        # 的 release_on_failure（失败出口仅当本轮 acquire 过才解闩）
                        messages, _compacted = on_compression_request(messages, turn, gate_acquired)
                    except Exception:
                        # P2a：回调异常防护（贴 on_before_llm 风格）——logger.exception +
                        # 解闩防闩锁滞留，继续落回原发送（不炸生成器、无 chat_idle 丢失）；
                        # _compacted=False 落入下方"未完成"分支（manual 重设/auto 记 warning）
                        logger.exception("[AgentLoop] on_compression_request callback failed")
                        _gate_release_if_acquired(gate_acquired)  # 失败解闩防闩锁滞留
                        _compacted = False
                    # R5-A P2-2：压缩整体替换丢弃当轮动态块 → 重跑 on_before_llm 幂等重插
                    if _compacted and on_before_llm is not None:
                        try:
                            on_before_llm(messages, turn)
                        except Exception:
                            logger.exception("[AgentLoop] on_before_llm re-run after compression failed")
                    # R5-A P2-1 + quality 微修：压缩未完成（manual/auto/回调异常）→ 一律解闩
                    # 防进程级永久失效；manual 重设意图 + 告知（用户显式请求不丢），
                    # auto 不重设（下轮 ratio_hit 再试——ratio 未回落则 gate_acquired 重新置闩）
                    if not _compacted:
                        if had_intent and _reason == "manual":
                            logger.warning("[Compression] manual compression did not complete, re-requesting")
                            request_compression("manual")
                            yield StreamEvent("system", "压缩未完成（提炼冷却或失败），将自动重试")
                        else:
                            # P1：auto 达线但压实失败（如提炼未完）——不重设意图，下轮 ratio_hit 再试；
                            # 必须解闩防闩锁滞留导致本 loop 后续永久失效
                            logger.warning("[Compression] auto compression did not complete, releasing gate for next-round retry")
                        _gate_release_if_acquired(gate_acquired)  # 失败解闩防卡死（幂等）
                    # R2-A P1-2：压缩成功不置冷却（AUTO_GATE 滞回 +
                    # 压后估算回落天然防风暴）；滞回 release 由 run_controlled_compression
                    # 压后估算回落决定（成功保持闩锁直至回落）
        response_gen = client.chat(messages=messages, tools=tools_schema)
        if verbose:
            response = yield from response_gen
            # === stream_error 检查（优先级最高，在 B1/拦截/reply yield 之前）===
            if getattr(response, 'stream_error', False):
                # E2 源头友好化：函数内局部导入防循环依赖（agent_loop→litellm_adapter→runner→agent_loop 环）
                from agent.generic.litellm_adapter import format_llm_error_for_user
                error_msg = getattr(response, 'error_msg', None) or "模型调用失败"
                yield format_llm_error_for_user(error_msg, getattr(response, "error_type_name", None))
                yield StreamEvent("system", "chat_idle")
                if not getattr(handler, "_is_subagent", False):
                    clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
                _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
                return {"result": "LLM_ERROR", "error_msg": error_msg, "error_type": getattr(response, "error_type_name", None)}
            yield StreamEvent("system", "\n\n")
        else:
            response = exhaust(response_gen)
            # === stream_error 检查（优先级最高，在 B1/拦截/reply yield 之前）===
            if getattr(response, 'stream_error', False):
                # E2 源头友好化：函数内局部导入防循环依赖（agent_loop→litellm_adapter→runner→agent_loop 环）
                from agent.generic.litellm_adapter import format_llm_error_for_user
                error_msg = getattr(response, 'error_msg', None) or "模型调用失败"
                yield format_llm_error_for_user(error_msg, getattr(response, "error_type_name", None))
                yield StreamEvent("system", "chat_idle")
                if not getattr(handler, "_is_subagent", False):
                    clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
                _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
                return {"result": "LLM_ERROR", "error_msg": error_msg, "error_type": getattr(response, "error_type_name", None)}
            # 过滤掉 <tool_use> 标签，只返回纯文本
            content = response.content or ""
            content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)

            # === 截断重试（B1）===
            if getattr(response, 'finish_reason', None) == "length":
                if _truncation_retry_count < _max_truncation_retries:
                    _truncation_retry_count += 1
                    if on_turn_end is not None:
                        tools_schema = on_turn_end(messages, tools_schema, turn)
                    messages.append({"role": "assistant", "content": response.content or ""})
                    messages.append({"role": "user", "content":
                        "你的上一轮输出因超过最大长度被自动截断，内容不完整。"
                        "请大幅缩短你的输出，只保留核心内容，确保输出完整结束。"
                        "如果内容确实很长，请先用 write 工具写入文件，再返回文件路径摘要。"
                    })
                    yield StreamEvent("system", "⚠️ 输出超长被截断，正在重试...\n")
                    logger.warning(f"[AgentLoop] Output truncated (finish_reason=length), retry {_truncation_retry_count}/{_max_truncation_retries}")
                    continue
                else:
                    logger.warning(f"[AgentLoop] Output truncated after {_max_truncation_retries} retries, force exit")
                    yield StreamEvent("system", "⚠️ 输出多次超长截断，已强制退出\n")
                    if on_turn_end is not None:
                        on_turn_end(messages, tools_schema, turn)
                    if not getattr(handler, "_is_subagent", False):
                        clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
                    yield StreamEvent("system", "chat_idle")
                    return {"result": "CURRENT_TASK_DONE", "data": None,
                            "messages": messages, "finish_reason": "length"}
            else:
                _truncation_retry_count = 0  # 非截断响应重置重试预算

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
                    # @end 允许退出：@end 前 + @end 后拼接（标记剥掉——T1 边界修复），
                    # 空值兜底原始 content——_compute_exit_content 纯函数计算
                    stripped_content = content.lstrip()
                    at_end_idx = _find_unescaped_marker(stripped_content, "@end")
                    if at_end_idx >= 0:
                        exit_content = _compute_exit_content(stripped_content, at_end_idx, content)
                    else:
                        exit_content = content
                    yield StreamEvent("reply", exit_content)
                    # 超长检测：非程序触发子 Agent 的 @end 报告超 2000 字符时写文件
                    if len(exit_content) > 2000 and not getattr(handler, '_program_triggered', False):
                        unique_name = getattr(handler, '_subagent_unique_name', 'unknown')
                        N = len(exit_content)
                        try:
                            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                            filename = f'{timestamp}-{unique_name}.md'
                            filepath = get_tmp_dir() / filename
                            filepath.write_text(exit_content, encoding='utf-8')
                            exit_content = f'{unique_name} 工作已完成，因信息内容共 {N} 字符已超限，存入以下文件：{filepath}'
                            yield StreamEvent("reply", exit_content)  # 文件路径提示覆盖 last_reply
                        except Exception as e:
                            logger.warning(f'[SubAgent] Failed to save overlength report to file: {e}')
                            # 写文件失败：exit_content 保持完整内容，跳过第二次 yield
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
                # 缓存命中捕获：usage.cached_tokens（litellm 归一化后的 prompt 缓存命中数；
                # 服务端未返回时置 0——get_stats 据此给 None 而非 0%）
                # 服务端未返回→None=未知；返回 0 是真实零命中，保留 0——get_stats 如实区分
                _cached = u.get('cached_tokens') if isinstance(u, dict) else getattr(u, 'cached_tokens', None)
                try:
                    handler._last_cached_tokens = int(_cached) if isinstance(_cached, (int, float)) else None
                except (TypeError, ValueError):
                    handler._last_cached_tokens = None
                # 校准倍率更新（Task 3/D9）：倍率=真值÷完整发送集全量 count，每次响应覆盖更新。
                # 响应返回点 messages 即完整发送集（system/动态块/索引——_on_before_llm 原地插入），
                # 原地改写不再漂移；每响应全量 count 一次，无增量缓存。
                # 仅主 Agent（子 Agent 可能挂副模型，混入会污染主上下文预算倍率）；
                # >0 守卫：服务端无 usage 时白算省掉；轻量 try/except——校准失败绝不影响主循环。
                if not getattr(handler, '_is_subagent', False) and last_prompt_tokens > 0:
                    try:
                        from agent.context_assembler.calibration import update_ratio
                        update_ratio(last_prompt_tokens, count_messages_tokens(messages))
                    except Exception:
                        pass
                logger.info(f"[Context] prompt_tokens={last_prompt_tokens}, context_window={context_window_tokens}")
                # 提取后立即检测：子 Agent 超阈值在当前轮做占位符化/FIFO（无工具调用时
                # 循环会退出，下轮顶部检测不会执行，所以此处必须检测）。
                # 主 Agent（_is_subagent=False）不再响应后压实——压缩已迁发送前门
                # （spec 2026-09-06 统一压缩入口）。
                if context_window_tokens > 0:
                    usage_ratio = last_prompt_tokens / context_window_tokens
                    if usage_ratio > warning_threshold:
                        if getattr(handler, "_is_subagent", False):
                            # 子 Agent：阶段 1 tool 占位符化 → 仍超才阶段 2 FIFO 兜底
                            target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.50)
                            replaced = _placeholderize_tool_outputs(messages, target_tokens)
                            if count_messages_tokens(messages) > target_tokens:
                                removed = _fifo_prune(messages, target_tokens, is_resumed=(resumed_messages is not None))
                                if removed > 0:
                                    logger.info(f"[FIFO] Proactive pruning: {last_prompt_tokens}/{context_window_tokens} tokens "
                                                f"({usage_ratio:.1%} > {warning_threshold:.0%}), removed {removed} messages, "
                                                f"now ~{count_messages_tokens(messages)} tokens (target {target_tokens})")
                            elif replaced > 0:
                                logger.info(f"[ToolCrop] placeholderized {replaced} tool outputs, "
                                            f"now ~{count_messages_tokens(messages)} tokens (target {target_tokens})")
        else:
            logger.debug(f"[Context] No usage in response: hasattr={hasattr(response, 'usage')}, usage={getattr(response, 'usage', 'N/A')}")

        # 检测 LLM 返回的 context_length_exceeded 标记（覆盖 verbose=True 和 verbose=False）
        if hasattr(response, 'context_overflow') and response.context_overflow:
            logger.warning("[Overflow] LLM API returned context_length_exceeded, triggering CONTEXT_OVERFLOW")
            if on_turn_end is not None:
                on_turn_end(messages, tools_schema, turn)
            if not getattr(handler, "_is_subagent", False):
                clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
            _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
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

        # === 截断重试（B1）— 统一路径（覆盖 verbose=True）===
        if getattr(response, 'finish_reason', None) == "length":
            if _truncation_retry_count < _max_truncation_retries:
                _truncation_retry_count += 1
                if on_turn_end is not None:
                    tools_schema = on_turn_end(messages, tools_schema, turn)
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append({"role": "user", "content":
                    "你的上一轮输出因超过最大长度被自动截断，内容不完整。"
                    "请大幅缩短你的输出，只保留核心内容，确保输出完整结束。"
                    "如果内容确实很长，请先用 write 工具写入文件，再返回文件路径摘要。"
                })
                yield StreamEvent("system", "⚠️ 输出超长被截断，正在重试...\n")
                logger.warning(f"[AgentLoop] Output truncated (finish_reason=length), retry {_truncation_retry_count}/{_max_truncation_retries}")
                continue
            else:
                logger.warning(f"[AgentLoop] Output truncated after {_max_truncation_retries} retries, force exit")
                yield StreamEvent("system", "⚠️ 输出多次超长截断，已强制退出\n")
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                if not getattr(handler, "_is_subagent", False):
                    clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
                yield StreamEvent("system", "chat_idle")
                return {"result": "CURRENT_TASK_DONE", "data": None,
                        "messages": messages, "finish_reason": "length"}
        else:
            _truncation_retry_count = 0  # 非截断响应重置重试预算

        # 如果在 LLM 流式传输期间请求停止，跳过部分 tool_calls 处理
        if stop_predicate():
            logger.info("[AgentLoop] Stop requested after LLM stream, skipping tool calls")
            if not getattr(handler, "_is_subagent", False):
                clear_stop()  # 主 Agent 自己消费停止意图
            # 子 Agent（_is_subagent=True）不清全局标志——被主 Agent 停止意图打断时保留给主 Agent 消费
            _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}

        tool_results = []
        next_prompts = set()
        should_exit = None

        if not response.tool_calls:
            tool_calls = [{"tool_name": "no_tool", "args": {}}]
        else:
            # P0-6: 添加 JSON 解析异常处理
            _parse_fail_count = 0  # E4-01：轮起点重置——触发严格限定"同一轮连续 3 次"（防纯文本/成功轮不清零跨轮累计提前截断 LLM 自纠）
            tool_calls = []
            for tc in response.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    tool_calls.append({
                        "tool_name": tc.function.name,
                        "args": args,
                        "id": tc.id,
                    })
                    _parse_fail_count = 0  # E4-01：解析成功时计数清零（同一轮内成功后失败重新计数）
                except json.JSONDecodeError as e:
                    # E4-01：不再 append {"args": {}} 空参继续（空参调用会产生误导性结果——
                    # 如 do_edit 空参）——①构建错误工具结果直接进 tool_results（跳过 dispatch，
                    # tc['error'] 在此消费）；②同错误文本注入 next_prompts 循环续行（防全失败轮
                    # len(next_prompts)==0 走 CURRENT_TASK_DONE 退出——LLM 下一轮可见可自纠）。
                    _parse_fail_count += 1
                    err_text = f"[工具参数解析失败: {e}]"
                    if len(err_text) > 500:  # 截断保尾 ≤500（复用 E1 错误格式规范）
                        err_text = err_text[: 500 - (len("...") + 100)] + "..." + err_text[-100:]
                    logger.error(f"[ERROR] Failed to parse tool arguments for {tc.function.name}: {e}")
                    logger.error(f"[ERROR] Raw arguments: {tc.function.arguments}")
                    if _parse_fail_count >= _max_parse_failures:
                        # 同一轮连续 3 次解析失败：第 3 次不再注入 next_prompts（错误工具结果已可见），
                        # 显式退出（对齐截断强制退出 L1131-1140 模式——⚠️ system → chat_idle → return）。
                        # ⚠️ 提示先行：退出路径用户侧可见不静默；不落库本轮 tool_results——return 发生在
                        # 工具结果 flush/persist 之前，保持丢弃语义（防孤儿 tool 消息）
                        logger.warning(f"[AgentLoop] Failed to parse tool arguments {_max_parse_failures} times consecutively, force exit")
                        yield StreamEvent("system", "⚠️ 工具参数连续 3 次解析失败，已强制退出\n")
                        if on_turn_end is not None:
                            on_turn_end(messages, tools_schema, turn)
                        if not getattr(handler, "_is_subagent", False):
                            clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
                        yield StreamEvent("system", "chat_idle")
                        return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}
                    tool_results.append({
                        "tool_use_id": tc.id,
                        "content": err_text,
                        "tool_name": tc.function.name,
                    })
                    next_prompts.add(err_text)

        # @指令跳过提示（2026-09-03 D1-D4）：@指令与工具调用同轮时工具优先执行、@指令被静默跳过——
        # 注入 next_prompts 随下轮引导块送达（截断免疫；E4-01/L1447 先例位置）。仅子 Agent 路径，
        # _bypass_at_prefix 严格 is not True 对齐拦截层先例（宽松判断会把 MagicMock handler 误判绕过）
        if response.tool_calls and getattr(handler, "_is_subagent", False) and getattr(handler, "_bypass_at_prefix", False) is not True:
            skipped = _detect_skipped_at_directive(response.content)
            if skipped:
                next_prompts.add(skipped)

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
            if stop_predicate():
                logger.info("[AgentLoop] Stop requested, skipping remaining tools")
                if not getattr(handler, "_is_subagent", False):
                    clear_stop()  # 主 Agent 自己消费停止意图
                # 子 Agent（_is_subagent=True）不清全局标志——被主 Agent 停止意图打断时保留给主 Agent 消费
                _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
                yield StreamEvent("system", "chat_idle")
                return {"result": "STOPPED", "messages": messages}
            gen = handler.dispatch(tool_name, args, response, index=ii)
            if verbose:
                yield StreamEvent("tool_marker", "`````\n")
                outcome = yield from gen
                yield StreamEvent("tool_marker", "`````\n")
            else:
                # 可中断工具执行：后台线程消费 dispatch generator，前台轮询 stop_predicate。
                # stop 置位 → 放弃等待（后台线程继续跑完，结果丢弃——用户拍板"后台去运行好了"）。
                # dispatch generator 的事件 yield（tool_marker/system）在非 verbose 下本就由
                # exhaust 丢弃（现状行为），后台消费不改变可见性。
                _completed, _outcome = run_interruptibly(
                    exhaust, stop_predicate, args=(gen,),
                )
                if not _completed:
                    logger.info("[AgentLoop] Stop requested during tool execution, abandoning wait")
                    # R1-P1-1（双审查交叉）：chat-with-* 同步子 Agent 内联在 dispatch generator 里
                    # （handler.py L1251-1265 通配路由 → _call_subagent_gen），后台线程继续消费 gen
                    # 时子 Agent loop 的 stop_predicate=(global or terminate_event)——下面 clear_stop()
                    # 清全局后谓词只剩 terminate_event（未置位）→ 子 Agent 逃逸单击停止跑完全程。
                    # 修复：放弃分支先 terminate 该子 Agent 实例（terminate_event.set()，让子 Agent
                    # LLM 流式/循环检查点 ≤0.2s 停止）。
                    if tool_name.startswith("chat-with-"):
                        _agent_name = tool_name[len("chat-with-"):]
                        try:
                            from agent.subagent_registry import SubagentRegistry
                            _inst = SubagentRegistry.get(_agent_name)
                            if _inst is None:
                                logger.warning(f"[AgentLoop] chat-with subagent {_agent_name} not found at abandon (escape risk)")
                            else:
                                _ev = getattr(_inst, "terminate_event", None)
                                if _ev is None:
                                    logger.warning(f"[AgentLoop] chat-with subagent {_agent_name} terminate_event missing at abandon (escape risk)")
                                else:
                                    _ev.set()
                                    logger.info(f"[AgentLoop] Terminated subagent {_agent_name} on tool-abandon")
                        except Exception as _e:
                            logger.warning(f"[AgentLoop] Failed to terminate subagent {_agent_name}: {_e}")
                    if not getattr(handler, "_is_subagent", False):
                        clear_stop()  # 主 Agent 自己消费停止意图
                    _clear_compression_intent_on_abnormal_exit(handler)  # B-P2-1：异常退出清未消费压缩意图
                    yield StreamEvent("system", "chat_idle")
                    return {"result": "STOPPED", "messages": messages}
                outcome = _outcome

            # === 统一截断关口 ===
            # 距离 Agent 调用最近，覆盖所有工具路径（MCP/disk/内置/chat-with-*）
            # 前端 API 和内部业务（region_detector/region_manager）不经过 dispatch，不被截断
            if outcome.data is not None:
                if isinstance(outcome.data, dict):
                    outcome.data = _truncate_dict_result(outcome.data, tool_name)
                elif isinstance(outcome.data, list):
                    # list 类型：序列化后截断，返回 truncated dict（与 _truncate_dict_result 一致）
                    try:
                        _list_str = json.dumps(outcome.data, ensure_ascii=False, default=json_default)
                    except Exception:
                        # E4-15：list 序列化失败（如自引用循环 ValueError）→ 错误 dict 兜底（防整轮失败）
                        outcome.data = {"error": f"[工具结果序列化失败: {type(outcome.data).__name__}]"}
                    else:
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
                        datastr = _serialize_tool_result_data(outcome.data)
                        _entry = {"tool_use_id": tid, "content": datastr, "tool_name": tool_name}
                        tool_results.append(_entry)
                    else:
                        # E4-03：data=None → 中性占位（无错误前缀语义）
                        tool_results.append({"tool_use_id": tid, "content": "（工具已执行，无返回值）", "tool_name": tool_name})
                # 添加tool消息到messages
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        # 冗余截断（统一关口已在 dispatch 后截断 outcome.data），保留作防御性编程
                        "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
                    }
                    # plan 2026-09-10 D8：删 name 字段注入（规范 tool 消息键集仅 role/content/tool_call_id）
                    messages.append(tool_msg)
                # V4: yield每条tool结果的persist事件（fold 成功结果照常落库——LLM 需见"我折过了"记录防循环折叠）
                for tool_result in tool_results:
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_result["tool_use_id"],
                        "content": tool_result["content"]
                    }
                    yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                if not getattr(handler, "_is_subagent", False):
                    clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
                yield StreamEvent("system", "chat_idle")
                return {
                    "result": "EXITED",
                    "data": outcome.data,
                    "messages": messages,
                }  # should_exit is only used for immediate exit
            if outcome.next_prompt.startswith("未知工具") or outcome.next_prompt.startswith("Unknown tool"):
                client.last_tools = ""

            # 关键：Anthropic API 要求每个 tool_call 都有 tool_result
            # 即使 outcome.data 为 None，也必须添加 tool_result
            # 但 no_tool 场景 tid 为空字符串，不应产生孤立的 tool 消息
            if tid:
                if outcome.data is not None:
                    datastr = _serialize_tool_result_data(outcome.data)
                    _entry = {"tool_use_id": tid, "content": datastr, "tool_name": tool_name}
                    tool_results.append(_entry)
                else:
                    # E4-03：data=None → 中性占位（无错误前缀语义）
                    tool_results.append({"tool_use_id": tid, "content": "（工具已执行，无返回值）", "tool_name": tool_name})

            next_prompts.add(outcome.next_prompt)

        # 添加tool消息（工具结果）
        for tool_result in tool_results:
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                # 冗余截断（统一关口已在 dispatch 后截断 outcome.data），保留作防御性编程
                "content": _truncate_tool_content(tool_result["content"], tool_result.get("tool_name", "")),
            }
            # plan 2026-09-10 D8：删 name 字段注入（规范 tool 消息键集仅 role/content/tool_call_id）
            messages.append(tool_msg)
        # V4: yield每条tool结果的persist事件（fold 成功结果照常落库——LLM 需见"我折过了"记录防循环折叠）
        for tool_result in tool_results:
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": tool_result["content"]
            }
            yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))

        # M2-F1 fold 清零：本轮任一 fold_tool_output 成功 → 清 LLM 真值缓存（贴压实清零先例）——
        # fold 后旧 prompt_tokens/cached_tokens 已失效，动态块落估算兜底；rebuild hook 紧跟其后
        # 重建 _fold_stats。content 非 JSON/非 dict/解析失败 → 跳过不清（不中断循环）。
        for tool_result in tool_results:
            if tool_result.get("tool_name") != "fold_tool_output":
                continue
            try:
                _fr = json.loads(tool_result["content"])
                if (isinstance(_fr, dict) and _fr.get("status") == "ok"
                        and isinstance(_fr.get("folded"), list) and len(_fr["folded"]) > 0):
                    handler._last_prompt_tokens = 0
                    handler._last_cached_tokens = None
                    last_prompt_tokens = 0  # 同步清循环局部（FinalReview P3-2）——折叠后视图已瘦身，
                    # 下轮 80% 检测若仍用折叠前旧真值会对无需压实视图做一次不必要 in-loop 压实
                    logger.debug("[AgentLoop] fold_tool_output ok, cleared LLM token truth cache")
            except Exception:
                logger.debug("[AgentLoop] fold result unparseable, skip clearing token cache")

        # 每工具轮视图重建（2026-09-02）：任何工具结果 persist（yield 即落库）后从 DB
        # 全量重建视图并原地替换 messages——新输出编号/折叠态/仪表盘与 DB 同步（fold 只
        # UPDATE DB，内存视图不感知，不刷新则同循环下轮仍见折叠前原文与旧使用率）。
        # tool_results 守卫：纯文本轮走 no_tool 占位路径不触发；触发在 supplement drain
        # 之前（未落库 supplement 的重建盲区最小化，R1-B P3 承认边界）；子 Agent
        # on_tool_round_refresh=None 跳过。失败不中断循环——下轮入口组装自然自愈。
        if tool_results and on_tool_round_refresh is not None:
            try:
                on_tool_round_refresh(messages)  # 原地 messages[:] 替换（与压实回调同制式）
            except Exception:
                logger.exception("[AgentLoop] on_tool_round_refresh failed, next entry assembly will self-heal")

        if len(next_prompts) == 0:
            if len(handler._done_hooks) == 0:
                # 同步子 Agent 挂起警告（2026-08-31 用户拍板）：仅同步挂起；拦截式注入，
                # LLM 同循环内可见；不 yield persist（依赖 persist_agent_reply role=user skip 不进 db）
                if not getattr(handler, "_is_subagent", False) and not _sync_suspend_warned:
                    from agent.subagent_registry import SubagentRegistry
                    # 只警告主 Agent 自己调起的同步挂起（source="user"/"scheduler"）——程序触发子 Agent
                    # （睡眠管道等，source="program"）挂起残留与主 Agent 无关，不警告（误警告会每轮打扰主 Agent）
                    _pending_sync = [
                        _inst for _inst in SubagentRegistry.list_running()
                        if getattr(_inst, "is_sync", False)
                        and getattr(_inst, "state", None) == "waiting_for_answer"
                        and getattr(_inst, "source", "user") != "program"
                    ]
                    if _pending_sync:
                        _sync_suspend_warned = True
                        _names = "、".join(_inst.unique_name for _inst in _pending_sync)
                        messages.append({
                            "role": "user",
                            "content": (
                                f"[系统警告] 同步进程仍在挂起等待你的回答：{_names}。"
                                "这一轮你没有调用工具，确定要退出这次的工具循环吗？这可能造成数据丢失。"
                            )
                        })
                        logger.warning(f"[AgentLoop] 主 Agent 结束工具循环但同步子 Agent 仍挂起: {_names}")
                        continue  # LLM 下一轮（同循环内）看到警告
                # 纯文本回复：也要执行衰减
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                # V4: 纯文本回复yield persist事件（从response.content构造，不从messages[-1]获取）
                if response.content and not response.tool_calls:
                    pure_text_msg = {"role": "assistant", "content": response.content}
                    yield StreamEvent("persist", json.dumps(pure_text_msg, ensure_ascii=False))
                # V4: 通知前端进入空闲状态
                if not getattr(handler, "_is_subagent", False):
                    clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
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
                if summary_response and getattr(summary_response, 'stream_error', False):
                    logger.warning(f"[Summary] LLM error, skipping summary: {summary_response.error_msg}")
                    summary_text = ''
                else:
                    summary_text = summary_response.content if summary_response else ''
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
            if not getattr(handler, "_is_subagent", False):
                clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
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
    if not getattr(handler, "_is_subagent", False):
        clear_stop()  # 子 Agent 任何路径退出不清全局标志（防止误清主 Agent 停止意图）
    yield StreamEvent("system", "chat_idle")
    return {"result": "MAX_TURNS_EXCEEDED", "messages": messages}

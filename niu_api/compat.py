"""
Compatibility API endpoints - matches the original Go API paths

These endpoints are used by the Electron UI (main.js).
"""

import asyncio
import json
import os
import re
import threading
import time
from asyncio import sleep as _asyncio_sleep
from concurrent.futures import Future
from datetime import datetime
from typing import TypedDict

from agent.session import get_message_store
from agent.subagent import (
    _read_context_window_tokens,
)
from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel

# 精灵（spirit）睡眠状态通道：Electron 主进程状态转换时转发（main.js spirit-state），
# tidy mode='sleep' 时冗余置位。整理管道 sleep 状态机（CP0-CP3）读取。
_SPIRIT_STATE = "idle"  # 默认非睡眠（安全方向）


def set_spirit_state(state: str) -> None:
    global _SPIRIT_STATE
    _SPIRIT_STATE = (state or "").lower()


def is_sleeping() -> bool:
    return _SPIRIT_STATE == "sleep"


def _is_subagent_overflow(result: str) -> bool:
    """检测子 Agent 是否因上下文溢出而退出（需匹配 overflow + agent + tokens_used 三个特征键）"""
    if not result or not result.strip().startswith("{"):
        return False
    try:
        data = json.loads(result)
        return (
            isinstance(data, dict)
            and data.get("overflow") is True
            and "agent" in data
            and "tokens_used" in data
        )
    except (json.JSONDecodeError, ValueError):
        return False


def _is_subagent_incomplete(result: str) -> bool:
    """检测子 Agent 是否因未完成终止（轮次耗尽 / 被停止 / supplement 终止）而退出。

    匹配 call_subagent 后处理返回的 incomplete JSON（{"incomplete": true, ...}）。
    严格 is True 判定：{"incomplete": false} / {"incomplete": "true"} 均不命中。
    纯文本 / overflow JSON / 畸形 JSON 均 False。
    """
    if not result or not result.strip().startswith("{"):
        return False
    try:
        data = json.loads(result)
        return isinstance(data, dict) and data.get("incomplete") is True
    except (json.JSONDecodeError, ValueError):
        return False


def _is_subagent_failure(result) -> bool:
    """子 Agent 程序化失败（注册冲突 '[错误]' / LLM 错误 'SUBAGENT_ERROR:'）——游标不得推进。"""
    return isinstance(result, str) and (
        result.startswith("[错误]") or result.startswith("SUBAGENT_ERROR:")
    )


def _incomplete_reason(result: str) -> str:
    """提取 incomplete JSON 的 reason（日志区分用）。非 incomplete 返回空串。"""
    try:
        data = json.loads(result) if result and result.strip().startswith("{") else None
    except (json.JSONDecodeError, ValueError):
        return ""
    if isinstance(data, dict) and data.get("incomplete") is True:
        return str(data.get("reason", "unknown"))
    return ""


def _extract_overflow_info(result: str) -> dict:
    """从子 Agent 溢出报告中提取信息"""
    try:
        return json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return {"overflow": True, "raw": result}


def _is_empty_shell_assistant(m) -> bool:
    """判断是否为空壳 assistant 消息（压缩残留）。

    同时满足才为 True（严格判断，防止误删合法消息）：
    1. role == assistant
    2. content 为空（strip 后空）
    3. tool_calls 为空（'[]' / [] / None——JSON 解析后空列表）
    4. tool_call_id 为空

    **原始形态（content 空但 tool_calls 非空）不满足条件 3 → 返回 False → 保留**
    （工具调用锚点——删除会导致多轮工具对话丢失工具结果上下文）
    """
    if getattr(m, "role", "") != "assistant":
        return False
    content = getattr(m, "content", "") or ""
    if content.strip():
        return False
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        try:
            tcs = json.loads(tcs) if isinstance(tcs, str) else tcs
        except (json.JSONDecodeError, TypeError):
            return False  # 解析失败 → 保守保留（防误删优先，与 Task 2 SQL 方向一致；
            # 注：生产路径 _safe_json 提前把坏 JSON 归 []，此分支是防御性——实际不触发）
    if tcs:  # 非空列表 = 工具调用锚点 → 保留
        return False
    tc_id = getattr(m, "tool_call_id", "") or ""
    if tc_id:
        return False
    return True


async def _cleanup_orphan_tool_messages(store):
    """清理 DB 中的畸形消息：孤立 tool 消息 + 空壳 assistant 消息。

    可能来源（历史压缩管道残留 / 异常中断）：
    1. 孤立 tool 消息——tool_call_id 无对应 assistant tool_calls
    2. 空壳 assistant 消息——tool 输出被级联删除后悬空清理清空其 tool_calls →
       留下 content 空 + tool_calls 空 的空壳

    **保留**：content 空但 tool_calls 非空的 assistant（工具调用锚点——agent_loop
    还原工具调用锚点、tool 消息靠 tool_call_id 归属）——绝不删除，防止工具结果上下文丢失。
    空壳 assistant 无任何语义，安全删除。
    """
    post_msgs = await store.get_messages()
    _valid_tc_ids: set[str] = set()
    for m in post_msgs:
        if getattr(m, "role", "") == "assistant":
            tcs = getattr(m, "tool_calls", None)
            if tcs:
                try:
                    if isinstance(tcs, str):
                        tcs = json.loads(tcs)
                    for tc in tcs:
                        tc_id = tc.get("id", "")
                        if tc_id:
                            _valid_tc_ids.add(tc_id)
                except (json.JSONDecodeError, TypeError):
                    pass
    _orphan_mids: list[str] = []
    for m in post_msgs:
        role = getattr(m, "role", "")
        if role == "tool":
            tc_call_id = getattr(m, "tool_call_id", "") or ""
            if tc_call_id and tc_call_id not in _valid_tc_ids:
                _orphan_mids.append(getattr(m, "id", ""))
        elif role == "assistant" and _is_empty_shell_assistant(m):
            # 空壳 assistant：content 空 + tool_calls 空 + tool_call_id 空——
            # 压缩残留，无任何对话/工具语义，安全删除
            _orphan_mids.append(getattr(m, "id", ""))
    if _orphan_mids:
        logger.info(f"[Tidy] Cleaning up {len(_orphan_mids)} orphan/empty-shell messages from DB")
        await store.delete_messages_by_ids(_orphan_mids)


def _build_compress_history(
    messages,
    msg_tokens: list | None = None,
    out_msg_ids: list | None = None,
) -> tuple[list[dict], dict[int, str]]:
    """构造带 [idx:N] 前缀的 history 列表 + idx↔UUID 映射。

    T6 保留（R4-A 边界）：压缩退役后暂无生产调用方，与后续 history 型输入可复用
    ——只摘除 protect_recent/exclude_protected 参数。

    与已退役的 journal 文件导出通道（直读 DB 改造前）的区别：
    - 输出 history 列表（role/content/tool_calls/tool_call_id 原样），而非序列化文本
    - content 开头加 `[idx:N] Ntokens ` 前缀（简易 idx，不用 UUID）
    - 单条 message 不会超限（每条就是原大小 + 前缀）

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content/tool_calls/tool_call_id）
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不加 tokens 前缀
        out_msg_ids: 输出参数，收集消息的真实 ID 列表（与 idx 顺序一致）

    Returns:
        (history, idx_to_id):
        - history: [{"role":..., "content": "[idx:N] Ntokens ...原content", "tool_calls"?:..., "tool_call_id"?:...}, ...]
        - idx_to_id: {idx: 真实 message_id}
    """
    if out_msg_ids is None:
        out_msg_ids = []

    history: list[dict] = []
    idx_to_id: dict[int, str] = {}
    display_idx = 0

    for rel_pos, msg in enumerate(messages):
        msg_id = getattr(msg, "id", "") or ""
        role = getattr(msg, "role", "user")
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)

        display_idx += 1
        out_msg_ids.append(msg_id)
        idx_to_id[display_idx] = msg_id

        # 构造前缀
        token_annotation = ""
        if msg_tokens and rel_pos < len(msg_tokens):
            token_annotation = f"{msg_tokens[rel_pos]}tokens "
        prefix = f"[idx:{display_idx}] {token_annotation}"

        # 构造 history entry（原样保留 role/tool_calls/tool_call_id）
        entry: dict = {"role": role, "content": prefix + content}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id

        history.append(entry)

    return history, idx_to_id


def _call_entity_extractor_on_f1(llm_config, f1_path=None) -> str:
    """v2 提炼调用：task 只含 F1 路径指令，不注入 history（睡眠专用）。"""
    from agent.md_mirror import F1_PATH
    from agent.subagent import call_subagent_with_auto_answer

    p = f1_path or F1_PATH
    task = (
        f"本次待提炼内容在文件 `{p}` 中。请按你的输入规范用 read 工具分段读取并提炼入库，"
        "完成后输出 @end 和 processed_line 行号。"
    )
    return call_subagent_with_auto_answer(
        agent_name="entity-extractor",
        task=task,
        llm_config=llm_config,
        mcp_client=None,
        context_fifo_threshold=-1,
    )


def _parse_and_relay_f1(entity_result: str, f1_path=None) -> int:
    """解析 processed_line 并触发 relay 剪切；解析失败告警并返回 0。"""
    m = re.search(r"processed_line\s*[=:\s]\s*(\d+)", entity_result or "")
    if m is None:
        logger.warning("[Tidy] entity-extractor 未输出 processed_line — F1 不剪切")
        return 0
    from agent.md_mirror import relay_processed_prefix
    return relay_processed_prefix(int(m.group(1)), f1_path)


def _call_dream_evolver_on_f3(llm_config, f3_path=None) -> str:
    """梦境调用：task 只含 F3 路径指令，不注入 history（睡眠循环专用，措辞仿 entity 版）。"""
    from agent.md_mirror import F3_PATH
    from agent.subagent import call_subagent_with_auto_answer

    p = f3_path or F3_PATH
    task = (
        f"本次待精加工的内容在文件 `{p}` 中。请按你的输入规范用 read 工具分段读取并完成知识图谱精加工，"
        "完成后输出 @end 和 processed_line 行号。"
    )
    return call_subagent_with_auto_answer(
        agent_name="dream-evolver",
        task=task,
        llm_config=llm_config,
        mcp_client=None,
        context_fifo_threshold=-1,
    )


def _parse_and_drop_f2(dream_result: str, f3_lines: int, f2_path=None) -> tuple[int, str]:
    """解析 processed_line 并触发 F2 前缀删除；纯同步。

    f3_lines 是 build_f3_from_f2 返回值由调用方透传的硬上界。
    """
    m = re.search(r"processed_line\s*[=:\s]\s*(\d+)", dream_result or "")
    if m is None:
        logger.warning("[Tidy] dream-evolver 未输出 processed_line — F2 不删除")
        return 0, ""
    from agent.md_mirror import drop_f2_prefix
    return drop_f2_prefix(int(m.group(1)), max_lines=f3_lines, f2_path=f2_path)

def truncate_message_content(content: str, max_chars: int = 500) -> str:
    """
    截断单条消息内容（用于雪球式压缩的 force 模式）。

    保留前 max_chars 个字符，附加截断标记和原始长度信息。
    子 Agent 可通过 get_messages 工具查看完整内容。

    Args:
        content: 原始消息内容
        max_chars: 保留的最大字符数

    Returns:
        截断后的内容（短于 max_chars 的内容不截断）
    """
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"...[截断，原内容{len(content)}字符，可用get_messages查看]"

def build_truncated_msg_list_text(
    messages,
    truncate: bool = False,
    max_chars: int = 500,
    max_messages: int = 0,
) -> str:
    """
    构建消息列表文本，可选截断单条消息内容和限制消息数量。

    Args:
        messages: 消息对象列表
        truncate: 是否截断消息内容（force 模式用 True）
        max_chars: 截断时保留的最大字符数
        max_messages: 最大消息数量。0 = 包含全部。设置后只保留最近 N 条，
            并在开头注明省略了多少条早期消息。

    Returns:
        格式化的消息列表文本
    """
    total = len(messages)
    omitted = 0
    if max_messages > 0 and total > max_messages:
        omitted = total - max_messages
        messages = messages[-max_messages:]

    lines = []
    if omitted > 0:
        lines.append(f"[省略了前 {omitted} 条消息，仅显示最近 {max_messages} 条]")
        lines.append("")

    for idx, msg in enumerate(messages, 1):
        msg_id = getattr(msg, "id", "") or ""
        content = msg.content or ""
        if truncate:
            content = truncate_message_content(content, max_chars=max_chars)
        lines.append(f"[id:{msg_id}] [idx:{idx}] {msg.role}: {content}")
    return "\n".join(lines)


def _estimate_total_tokens(messages) -> int:
    """估算消息列表的总 token 数（逐条计算，含角色开销）。

    tool 消息的 content 按 MAX_TOOL_RESULT_CHARS 截断，与 agent_loop 一致。
    """
    try:
        from agent.token_calculator import TokenCalculator
        calc = TokenCalculator.get()
        total = 0
        for msg in messages:
            content = getattr(msg, "content", "") or ""
            role = getattr(msg, "role", "user") or "user"
            if role == "tool" and len(content) > _MAX_TOOL_RESULT_CHARS:
                content = content[:_MAX_TOOL_RESULT_CHARS]
            total += calc.count_message_single(role, content, tool_calls=getattr(msg, "tool_calls", None))
        return total
    except Exception:
        from agent.subagent import count_tokens_for_text
        total_content = "".join(
            (m.content[:_MAX_TOOL_RESULT_CHARS] if getattr(m, "role", "") == "tool" and len(getattr(m, "content", "") or "") > _MAX_TOOL_RESULT_CHARS else (m.content or ""))
            for m in messages
        )
        return count_tokens_for_text(total_content)


async def compute_context_usage_estimate(store=None, context_window: int | None = None, messages=None) -> float | None:
    """全量估算当前上下文使用率（0-1）。

    与 get_stats 的 fallback 分支同源：读全量 messages.db → 本地 tokenizer 估算 →
    ÷ context_window。压缩完成后复用此函数重算并推送前端（旧 _last_prompt_tokens 失效）。
    返回 None 表示无法计算（异常或窗口配置无效），调用方应回退（前端走 loadStats 兜底），
    不要返回 0.0 伪装"空库"——0% 会误导前端渲染虚假低值。
    store/context_window/messages 可选注入：get_stats 已持有 store/窗口则传入避免重复读取；
    _compute_post_compress_usage 已取压缩后消息列表则传 messages 避免全表二次扫描。
    """
    try:
        if store is None:
            store = await get_message_store()
        if context_window is None:
            context_window = _read_context_window_tokens()
        if context_window <= 0:
            return None
        if messages is None:
            all_msgs = await store.get_messages()
        else:
            all_msgs = messages
        total_tokens = _estimate_total_tokens(all_msgs)
        return total_tokens / context_window
    except Exception:
        return None


async def _compute_post_compress_usage(store=None, msgs_before: int = -1) -> tuple[bool, float | None]:
    """压缩后判定：消息数减少（实际压缩）→ (True, 全量估算 usage)；否则 (False, None)。

    _tidy_context_impl finally 使用：仅"实际压缩"（删了消息）才重置 _last_prompt_tokens
    并推送新 usage；skip/abort/error 路径未压缩，旧真实 token 数仍有效必须保留
    （保留否则下次 sleep 判定切到偏低的估算基准，破坏 warningThreshold-0.1 冲突避让）。
    store 可选注入：调用方（_tidy_context_impl）已持有则传入，避免全表二次扫描；None 时自取。
    并发写入假阴性边界：压缩期间并发新增消息可能使计数不降（溢出 force + 用户活跃期），
    此时返回 False 不重置——活跃期前端 loadStats 显示 stale、真实自愈靠下次 LLM 交互
    更新 _last_prompt_tokens（agent_loop.py:994），见边界记录。
    纯 update 边界：keep-all + 只更新内容不删消息的压缩计数不变 → 判定未压缩 → 不 reset
    ——已知接受边界（罕见；估算略高估，不会触发错误压缩）。
    """
    try:
        if store is None:
            store = await get_message_store()
        after = await store.get_messages()
    except Exception:
        # store 读取失败：无法确认压缩是否发生，保守不 reset（不误重置未压缩场景）
        return False, None
    if len(after) < msgs_before:
        # compute 内部已吞异常返回 None → (True, None)：估算失败仍确认压缩、reset + 前端兜底
        usage = await compute_context_usage_estimate(store=store, messages=after)
        return True, usage
    return False, None


_tidy_lock = asyncio.Lock()

# 锁等待分片时长（§3.9）：helper 函数体内运行时读取，便于测试注入
TIDY_WAIT_CHUNK = 60.0


async def _acquire_chat_lock_with_retry(log_prefix: str, *, max_elapsed: float | None = None) -> bool:
    """无限心跳等待获取 _chat_lock（§3.9）。

    生产调用不传 max_elapsed——永不放弃（真排队无放弃，§3.1 统一不变量）；
    max_elapsed 仅测试注入，命中时 logger.error + return False。
    心跳日志仅在真实争用时出现（无争用一次 acquire 即成功，零日志）。

    Returns:
        True=已持有锁；False=仅测试注入 max_elapsed 超限放弃（生产不可达）。
    """
    start = time.monotonic()
    while True:
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=TIDY_WAIT_CHUNK)
            return True
        except TimeoutError:
            elapsed = time.monotonic() - start
            if max_elapsed is not None and elapsed >= max_elapsed:
                logger.error(f"[{log_prefix}] chat lock busy over {max_elapsed:.0f}s, giving up")
                return False
            logger.info(f"[{log_prefix}] chat lock busy, retrying ({elapsed:.0f}s elapsed)")


# ===========================================================================
# 全局整理队列（T2）：单 worker 串行执行整理类管道——全局一次一个、后来者排队
# 设计见 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.0-3.3
# ===========================================================================

class _PipelineItem(TypedDict):
    """整理管道任务项（§3.1）"""
    kind: str      # "sleep"（force/runner-force 已随 T6 压缩退役删除）
    request: dict  # _tidy_context_impl 的 request dict
    held: bool     # chat_lock_already_held（worker 透传）
    result: Future  # 一律携带，由 worker 完结（fire-and-forget 只是调用方不 await）


_pipeline_queue: asyncio.Queue | None = None
_pipeline_worker_task: asyncio.Task | None = None

async def _pipeline_worker():
    """全局整理队列单 worker（§3.3 伪代码逐字）"""
    while True:
        item = await _pipeline_queue.get()
        result = {"status": "error", "message": "not executed"}  # 预初始化
        try:
            kind = item["kind"]
            if kind == "sleep" and not is_sleeping():
                result = {"status": "cancelled", "reason": "woke_up"}  # CP0
            else:
                async with _tidy_lock:  # 双保险
                    result = await _tidy_context_impl(item["request"], chat_lock_already_held=item["held"])
        except Exception as e:
            logger.exception("[Pipeline] worker item failed")
            result = {"status": "error", "message": str(e)}
        finally:
            fut = item["result"]
            if not fut.done():
                fut.set_result(result)


def _pipeline_worker_guard(task: asyncio.Task) -> None:
    """worker 守护（done_callback）：CancelledError（shutdown）不重建；异常退出打 error 日志 + 重建。"""
    if task.cancelled() or not task.done():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.error(f"[Pipeline] worker crashed: {exc!r} — restarting")
    global _pipeline_worker_task
    if _pipeline_queue is None:
        logger.warning("[Pipeline] queue closed, skip worker restart")
        return
    _pipeline_worker_task = asyncio.ensure_future(_pipeline_worker())
    _pipeline_worker_task.add_done_callback(_pipeline_worker_guard)


def _pipeline_enqueue(kind: str, request: dict | None = None, held: bool = False) -> Future:
    """投递整理任务到全局队列，返回 concurrent.futures.Future（fire-and-forget 只是调用方不 await）。

    - None 窗口（队列未创建）：调用方按 §3.0 Option A 同步执行，本函数不处理。
    - 压缩类（force/runner-force）去重表已随 T6 压缩退役整体移除。
    """
    request = request or {}
    fut: Future = Future()
    item: _PipelineItem = {"kind": kind, "request": request, "held": held, "result": fut}
    try:
        _pipeline_queue.put_nowait(item)
    except Exception:
        # 调用方契约是返回 Future（chat_queue await wrap_future；None 会 TypeError），
        # 故投递失败重新 raise 而非返回 None。
        raise
    return fut


def start_pipeline_queue() -> None:
    """lifespan 启动：创建队列 + 启动 worker（幂等）。"""
    global _pipeline_queue, _pipeline_worker_task
    if _pipeline_queue is not None:
        return
    _pipeline_queue = asyncio.Queue()
    _pipeline_worker_task = asyncio.ensure_future(_pipeline_worker())
    _pipeline_worker_task.add_done_callback(_pipeline_worker_guard)


async def stop_pipeline_queue() -> None:
    """lifespan 关闭：排出队列剩余项（shutting down 异常）→ 取消 worker。"""
    global _pipeline_queue, _pipeline_worker_task
    queue = _pipeline_queue
    _pipeline_queue = None
    if queue is not None:
        while not queue.empty():
            item = queue.get_nowait()
            fut = item["result"]
            if not fut.done():
                fut.set_exception(RuntimeError("shutting down"))
    task = _pipeline_worker_task
    _pipeline_worker_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


router = APIRouter(tags=["compat"])

# 并发锁：串行化所有 chat 请求，防止并发调用 runner.chat() 导致共享状态损坏
_chat_lock = asyncio.Lock()


class ChatRequest(BaseModel):
    """Chat request"""

    message: str
    session_id: str | None = None
    resources: list = []
    source: str = ""


class ChatResponse(BaseModel):
    """Chat response"""

    reply: str
    session_id: str | None = None
    message_id: str | None = None


class MessageResponse(BaseModel):
    """Single message response"""

    id: str
    role: str
    content: str
    created_at: str


class MessagesResponse(BaseModel):
    """Messages list response"""

    messages: list[MessageResponse]
    total_in_db: int


class StatsResponse(BaseModel):
    """Stats response"""

    messages: int
    uptime: str
    files: int = 0      # 已处理文档数
    persons: int = 0    # 人物实体数
    notes: int = 0      # 笔记/知识实体数
    context_usage: float = 0.0  # 上下文使用率 0.0-1.0
    context_cache_hit: float | None = None  # 上下文缓存命中率 0.0-1.0；None=未知（服务端未返回 cached_tokens）


# Track startup time
_startup_time = datetime.now()

# Preload status
_preload_complete = False
_preload_stage = "正在启动服务"


def set_preload_complete():
    """Mark preload as complete"""
    global _preload_complete, _preload_stage
    _preload_complete = True
    _preload_stage = "启动完成"
    logger.info("Preload marked as complete")


def set_preload_stage(stage: str):
    """Set current preload stage text — called from lifespan at each phase.

    Writes to both a global variable (for /api/preload-status) and a file
    (~/.niu/.startup_stage) so the Rust launcher can read it BEFORE uvicorn
    starts accepting HTTP connections (lifespan runs before HTTP serve).
    Does NOT depend on logging config — always set regardless of log level.
    """
    global _preload_stage
    _preload_stage = stage
    logger.info(f"[STAGE] {stage}")
    # Write to file for Rust launcher to read during lifespan (pre-HTTP)
    try:
        from pathlib import Path
        stage_file = Path.home() / ".niu" / ".startup_stage"
        stage_file.write_text(stage, encoding="utf-8")
    except Exception:
        pass  # Non-blocking: HTTP fallback still works post-startup


@router.get("/api/llm-status")
async def get_llm_status() -> dict:
    """检测 LLM 是否已配置可用（三态，供启动器决策）。

    返回：
        ready: True = 配置存在 AND lifespan 探测通过（后端已真实连通性验证）
        probe_failed: True = 配置存在但 lifespan 探测失败（启动器需 test-llm 兜底
            区分慢模型/瞬态与真不通——配 llm.read_timeout 逃生口（wait≤220<230s）后
            慢模型可被 230s 验证放行；未配逃生口时 >120s 首字节慢模型后端 150s 强杀
            失败 → 配置页，与修复前一致）
        error: 失败原因（配置缺失或探测失败消息）
    """
    import json
    from pathlib import Path

    from niu_api.config import CONFIG_PATH
    from niu_api.internal.lightrag_manager import get_llm_gate_ready

    config_path = Path(CONFIG_PATH)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})
        api_key = llm.get("apiKey", "")
        api_base = llm.get("apiBase", "")
        model = llm.get("model", "")

        if not api_key:
            return {"ready": False, "probe_failed": False, "error": "API key not configured"}
        if not api_base or not model:
            return {"ready": False, "probe_failed": False, "error": "API base or model not configured"}
        if not get_llm_gate_ready():
            return {"ready": False, "probe_failed": True, "error": "LLM connectivity probe failed at startup"}
        return {"ready": True, "probe_failed": False}
    except Exception as e:
        return {"ready": False, "probe_failed": False, "error": str(e)}


async def _probe_llm(
    config: dict,
    *,
    read_timeout: float = 120.0,
    wait_timeout: float = 150.0,
) -> tuple[bool, str]:
    """真实 LLM 调用探测：验证 config → LiteLLM → provider 路由 → API 调用 → 响应。

    供 /api/test-llm 端点与启动 LLM 门控复用（同一预算——v2.2：read_timeout=120
    覆盖代码库显式支持的 20-120s 首响应推理模型；短预算会误杀慢首响模型，
    且与启动器 test-llm 判定分歧导致静默降级）。
    返回 (success, message_or_error)；异常消息已脱敏（key=*** / Bearer ***）。
    """
    from agent.generic.litellm_adapter import LiteLLMSession

    # 入口键名归一化（settings 表单可能传 apiKey/apiBase 原始大写键）
    config = {k.lower(): v for k, v in config.items()}

    # 判空（Ollama 等本地模型 apiBase 为 localhost/127.0.0.1 时 apiKey 豁免）
    apibase = config.get("apibase", "")
    is_local = (
        apibase.startswith("http://localhost")
        or apibase.startswith("http://127.0.0.1")
        or apibase.startswith("https://localhost")
        or apibase.startswith("https://127.0.0.1")
    )
    if not config.get("apikey") and not is_local:
        return False, "API Key 未配置"
    if not config.get("apibase"):
        return False, "API 地址未配置"
    if not config.get("model"):
        return False, "模型名称未配置"

    try:
        llm_config = {
            "api_type": config.get("type", "openai"),
            "apikey": config["apikey"],
            "apibase": config["apibase"],
            "model": config["model"],
            # 探测与生产同参数（组件 3）：reasoning_effort 从配置透传（不再硬编码 None）——
            # chat() 内 assemble_request_params 注入 extra_body 送达；thinking 随
            # litellm_kwargs 同源透传
            "reasoning_effort": config.get("reasoning_effort"),
            "provider": config.get("provider", ""),
            # 用户配置 max_tokens 时用用户值（testAndSave 顺带校验合法性：非法值 → 服务端 400 → probe 报错阻断保存）；
            # 无配置保持 256（探测提速——max_tokens 是上限非目标，不影响 "hi" 探测速度）
            "litellm_kwargs": {**config.get("litellm_kwargs", {}), "max_tokens": config.get("max_tokens") or 256},
            "read_timeout": read_timeout,
        }
        session = LiteLLMSession(cfg=llm_config)

        def _sync_test():
            gen = session.chat(messages=[{"role": "user", "content": "hi"}])
            chunks = []
            mock_resp = None
            truncated = False
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                mock_resp = e.value
            # 思考模型可能只输出 reasoning_content 而无文本 chunk，
            # 但 MockResponse 会包含 thinking/content 字段
            # stream_error=True 表示流式传输中途出错，partial content 不可信
            if mock_resp and getattr(mock_resp, "stream_error", False):
                return "", False, False  # 无内容，触发"模型返回空响应"错误提示
            # finish_reason=length：输出被 max_tokens 截断——必须报错（曾静默判通过）
            if mock_resp is not None and getattr(mock_resp, "finish_reason", None) == "length":
                truncated = True
            text = "".join(chunks)
            has_content = bool(text.strip()) or (
                mock_resp is not None
                and (getattr(mock_resp, "content", None) or getattr(mock_resp, "thinking", None))
            )
            return text, has_content, truncated

        # 外层超时给 read_timeout + 重试留余量（推理模型首响应慢）
        result, has_content, truncated = await asyncio.wait_for(asyncio.to_thread(_sync_test), timeout=wait_timeout)
        if truncated:
            return False, "模型输出被 max_tokens 截断（思考链可能消耗过多输出 token），建议关闭思考链或调大 max_tokens"
        if not has_content:
            return False, "模型返回空响应"

        provider = config.get("provider", "") or config.get("type", "openai")
        return True, f"模型测试通过 (model={config.get('model')}, provider={provider})"
    except TimeoutError:
        return False, "连接超时，请检查网络和 API 地址"
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "unauthorized" in error_msg.lower() or "invalid api key" in error_msg.lower():
            return False, "API Key 无效或未授权"
        if "404" in error_msg or "not found" in error_msg.lower():
            return False, "模型或 API 端点不存在，请检查模型名称和地址"
        # Sanitize error message to avoid leaking API keys in URLs/headers
        import re
        safe_msg = re.sub(r"key=[^&\s]+", "key=***", error_msg)
        safe_msg = re.sub(r"Bearer\s+[^\s]+", "Bearer ***", safe_msg)[:200]
        if "provider" in error_msg.lower() or "unmapped" in error_msg.lower():
            return False, f"Provider 路由错误: {safe_msg}"
        return False, f"模型测试失败: {safe_msg}"


@router.post("/api/config/reload")
async def reload_config() -> dict:
    """配置保存后热更新（免重启）：清除全部 LLM 相关缓存，下次使用按新配置重建。

    由设置窗口 save-config（Electron main.js IPC）写入 user-config.json 后调用。
    覆盖四层缓存：
    1. niu_api.config 全局 Config 单例（chat_session 的"LLM 未配置"检查读它）
    2. agent.runner 全局 Runner 单例（主 Agent LiteLLMSession 随 Runner 重建；
       进行中的回合持有旧实例引用不受影响，下一回合用新配置）
    3. lightrag_manager 缓存的 LiteLLMSession（LightRAG 链路）
    4. ChatQueue 的 runner 引用（定时任务链路——reload_runner 替换 self._runner，
       不清队列不重启 worker，待处理消息自动用新配置）

    子 Agent 无缓存无需处理（subagent.py 每次调用 create_client 新建，
    llm_config 随调用方传入）。
    """
    from niu_api import config as config_module

    config_module._config = None

    from agent import runner as runner_module

    with runner_module._runner_lock:
        runner_module._runner = None

    from niu_api.internal.lightrag_manager import reset_litellm_session_cache

    reset_litellm_session_cache()

    # 刷新 ChatQueue 的 runner 引用（定时任务链路——不能 _queue=None：
    # 新队列 worker 不会启动（start 仅应用启动时调用），消息会静默卡死）
    from niu_api import chat_queue as chat_queue_module

    q = chat_queue_module._queue
    if q is not None:
        q.reload_runner()

    logger.info("[ConfigReload] LLM caches cleared, next request uses new config")
    return {"success": True}


@router.post("/api/test-llm")
async def test_llm(request: Request) -> dict:
    """通过真实 LLM 调用验证配置。验证完整链路：config → LiteLLM → provider 路由 → API 调用 → 响应。

    请求体可选：传入 config 字典则用它测试（配置页面预保存测试）；
    不传或为空则从 user-config.json 读取（启动器验证）。
    """
    from niu_api.llm_ready import resolve_probe_budget
    from niu_api.llm_proxy import get_llm_config

    # 读取配置：优先用请求体，否则从文件读取
    try:
        body = await request.json()
    except Exception:
        body = {}

    # 统一键名为小写（前端传 apiKey/apiBase，get_llm_config 返回小写，需统一）
    body = {k.lower(): v for k, v in body.items()} if body else {}

    if body:
        # 用 body 测试（预保存测试）——body 非空即测 body（即使 apiKey 为空），
        # 与 probe 端点闸门语义对齐。回退读文件会掩盖被测问题
        # （如 Ollama 空 apiKey 表单场景，回退读到空配置导致 is_local 豁免不可达）。
        config = body
    else:
        # 启动器调用：从文件读取
        try:
            config = get_llm_config()
        except Exception as e:
            return {"success": False, "error": f"读取配置失败: {e}"}

    # body 已归一化，config 也需要确保小写
    config = {k.lower(): v for k, v in config.items()}

    # 预算解析：config.read_timeout 可覆盖默认（逃生口——>120s 慢模型通道，
    # 与启动门控 check_llm_ready 同一 helper，闭环）
    read_timeout, wait_timeout = resolve_probe_budget(config)
    # 启动器兜底路径（body 空，读已保存配置）走最小连通配置——启动探测只测连通性；
    # body 非空（testAndSave 预保存测试）保留用户参数校验组合可用性（用户需求 2026-08-20）。
    # 注意顺序：resolve_probe_budget 先消费完整 config（read_timeout 逃生口），再最小化。
    if not body:
        from niu_api.llm_ready import _minimal_probe_config
        config = _minimal_probe_config(config)
    success, message = await _probe_llm(
        config,
        read_timeout=read_timeout,
        wait_timeout=wait_timeout,
    )
    if success:
        return {"success": True, "message": message}
    return {"success": False, "error": message}


def _build_probe_response_format_json_schema() -> dict:
    """构造 Tier 1 探测用 response_format：json_schema strict，冲突式设计。

    schema 强制要求 {"verdict": "SCHEMA_ENFORCED"}，而探测 prompt（_build_probe_messages）
    要求模型写一句普通英文句子且禁止输出 JSON——两者矛盾。只有网关真正执行
    json_schema strict（schema 战胜 prompt）时，输出才会是 schema 约束的 JSON；
    网关静默接受但不执行时，模型跟随 prompt 输出普通句子，被判 gateway_blocked。

    Why 冲突式设计：2026-07-21 实测发现豆包 Coding Plan 网关行为是 flaky 的——
    同一请求 5 次采样，2 次 schema 胜、3 次 prompt 胜。原设计 prompt 与 schema
    都要求 {"ok": true}，模型跟随 prompt 即可输出合法 JSON，无法区分"真支持"
    与"静默忽略"，产生假阳性（碰巧命中执行窗口期时误判 json_schema）。
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "probe_response_format",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["SCHEMA_ENFORCED"]},
                },
                "required": ["verdict"],
                "additionalProperties": False,
            },
        },
    }


def _build_probe_response_format_json_object() -> dict:
    """构造 Tier 2 探测用 response_format：json_object。

    仅约束输出合法 JSON，不约束字段。
    """
    return {"type": "json_object"}


def _build_probe_messages() -> list[dict]:
    """构造探测消息：要求写一句普通英文句子且禁止输出 JSON。

    与 Tier 1 schema（强制 {"verdict": "SCHEMA_ENFORCED"}）故意矛盾——只有网关
    真正执行 response_format 时输出才是 JSON；网关静默忽略时模型跟随 prompt
    输出普通句子，被分类器判 gateway_blocked。

    Why 必须含 "json" 字样：OpenAI json_object 模式硬性要求 prompt 含 "json"
    字符串，否则直接 400（会造成对真支持厂商的假阴性）。"Do not output JSON"
    一句天然含 "JSON"，满足该检查。
    """
    return [{
        "role": "user",
        "content": "Write exactly one English sentence about the ocean. Do not output JSON.",
    }]


def _classify_probe_response_tier1(text: str) -> str:
    """Tier 1 (json_schema strict) 判定：响应必须是合法 JSON dict 且
    verdict == "SCHEMA_ENFORCED"（schema 战胜 prompt 的铁证）。

    容忍额外字段（部分厂商可能只严格执行 required/enum、宽松处理
    additionalProperties），但 verdict 值必须精确匹配枚举。

    真实环境验证（2026-07-21）：豆包 Coding Plan 网关行为 flaky——同一请求
    5 次采样，2 次 schema 胜、3 次 prompt 胜（模型跟随 prompt 输出海洋句子）。
    冲突式设计确保只有 schema 真正生效时才判 supported，消除假阳性。
    """
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return "gateway_blocked"
    if not isinstance(data, dict):
        return "gateway_blocked"
    if data.get("verdict") != "SCHEMA_ENFORCED":
        return "gateway_blocked"
    return "supported"


def _classify_probe_response_tier2(text: str) -> str:
    """Tier 2 (json_object) 判定：只要求响应是合法 JSON dict。

    探测 prompt 明确要求"不要输出 JSON"，此时输出仍是合法 JSON dict 即说明
    json_object 约束真正生效（模型被强制输出 JSON）；网关静默忽略时模型跟随
    prompt 输出普通句子 → 非 JSON → gateway_blocked。

    已知边界：理论上存在"网关静默忽略 + 模型不听指令仍输出 JSON"的假阳性
    组合，概率低且 json_object 档位误判代价小（运行时 json_repair 兜底）。
    """
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return "gateway_blocked"
    if not isinstance(data, dict):
        return "gateway_blocked"
    return "supported"


async def _probe_tier_three_samples_async(try_fn, response_format: dict) -> tuple[str, str]:
    """单档三次采样（异步版）：全过才 supported，限流/超时只重试不计失败。

    Args:
        try_fn: 单次采样异步函数，签名 () -> await (tier_result, raw_text_or_reason)。
                tier_result 取值: "supported" / "gateway_blocked" / "model_rejected" / "param_conflict" / "rate_limited" / "timeout" / "infra_error"
        response_format: 本档 response_format（用于日志）

    Returns:
        (result, last_raw) 元组：
        - result: "supported" / "gateway_blocked" / "model_rejected" / "param_conflict" / "rate_limited" / "infra_error"
        - last_raw: 最后一次采样的 raw 摘要（含异常类名，用于诊断；三次全过时为空字符串）

    Why 三次采样：2026-07-21 实测豆包 Coding Plan 网关行为非确定性（flaky），
    同一请求 5 次采样 2 次 schema 胜、3 次 prompt 胜。单次探测碰巧命中执行
    窗口期会误判 json_schema，碰巧命中静默忽略窗口期会误判 prompt_only。
    三次采样全过才升档——flaky 网关必然 ≥1 次静默忽略，稳定降级 prompt_only；
    真支持网关（OpenAI）3 次全过，稳定写入 json_schema。

    Why 限流/超时只重试不计失败：RateLimitError / litellm.Timeout /
    asyncio.TimeoutError ≠ 不支持，只是"这次请求被网关挡了"或"网关慢/抖动"。
    限流/超时同属 transient infra 问题，sleep 后重试本次采样，直到返回非限流/
    非超时结果（supported / model_rejected / gateway_blocked）才判定该次采样。

    Why 重试预算整档共享：防止限流/超时期间无限拖延端点。3 次采样共享 2 次
    重试预算（限流+超时累计），指数退避 5s→10s，最多等 15s。
    原 5 次预算（退避 155s）在思考链慢响应场景导致 probe 卡死 10+ 分钟，2 次足够覆盖瞬时抖动。
    """
    max_transient_retries = 2
    transient_retries = 0

    for sample_num in range(1, 4):
        while True:
            try:
                result, raw = await try_fn()
            except TimeoutError:
                result, raw = "timeout", "TimeoutError: 采样超时（30s）"

            if result in ("rate_limited", "timeout"):
                transient_retries += 1
                if transient_retries > max_transient_retries:
                    logger.warning(
                        f"探测限流/超时重试 {max_transient_retries} 次仍未成功，放弃 "
                        f"(最后错误: {result})"
                    )
                    return "rate_limited", raw
                sleep_seconds = 5 * (2 ** (transient_retries - 1))
                logger.info(
                    f"探测采样 {sample_num} {result}，{sleep_seconds}s 后重试 "
                    f"（第 {transient_retries} 次，response_format={response_format.get('type')}）"
                )
                await _asyncio_sleep(sleep_seconds)
                continue
            break

        if result == "infra_error":
            logger.warning(
                f"探测采样 {sample_num} 基础设施错误（{raw[:80]}），"
                f"不写配置，提示用户稍后重试"
            )
            return "infra_error", raw

        if result != "supported":
            logger.info(
                f"探测采样 {sample_num} 失败（{result}, response_format={response_format.get('type')}），"
                f"该档不通过"
            )
            return result, raw

    return "supported", ""


@router.post("/api/probe-response-format")
async def probe_response_format(request: Request) -> dict:
    """递进探测当前 LLM 配置对 response_format 的支持档位。

    前置条件：调用方（前端 testAndSave）必须先通过 /api/test-llm 连通性
    测试，确认 LLM 可正常对话。本端点假定连通性已验证，不再处理认证/网络
    等基础设施类错误（那些应该在连通性测试阶段就被拦截）。

    3 档递进（最强→最弱）：
    - Tier 1: json_schema strict → 三次采样全 verdict == "SCHEMA_ENFORCED" → json_schema
    - Tier 2: json_object → 三次采样全合法 JSON dict → json_object
    - Tier 3: 都失败 → prompt_only

    判定原则：冲突式设计 + 异常分类。
    - 冲突式设计：schema 强制要求 {"verdict": "SCHEMA_ENFORCED"}，prompt 要求
      "写海洋句子禁止 JSON"——只有 schema 战胜 prompt（网关真执行）才判
      supported，模型跟随 prompt 输出海洋句子即判 gateway_blocked。
    - 异常分类：
      * 没抛异常 + 响应符合该档要求 → "supported"
      * 没抛异常 + 响应不符合 → "gateway_blocked"
      * RateLimitError → "rate_limited"（限流，sleep 重试不计失败）
      * litellm.Timeout / asyncio.TimeoutError → "timeout"（超时，sleep 重试不计失败）
      * AuthenticationError / APIConnectionError / 5xx → "infra_error"
        （基础设施错误，端点早返 probe_failed 不写配置）
      * BadRequestError / UnsupportedParamsError → "param_conflict"
        （错误文案含 combination/reasoning_effort——参数组合无效，端点早返
        probe_failed 报"参数组合无效"阻断，不降级不保存）或 "model_rejected"
        （模型/网关明确拒绝，该档失败降级）
      * 其他异常 → "model_rejected"

    Why 三次采样：2026-07-21 实测豆包 Coding Plan 网关行为非确定性（flaky），
    同一请求 5 次采样 2 次 schema 胜、3 次 prompt 胜。单次探测碰巧命中执行
    窗口期会误判 json_schema，碰巧命中静默忽略窗口期会误判 prompt_only。
    三次采样全过才升档——flaky 网关必然 ≥1 次静默忽略，稳定降级 prompt_only；
    真支持网关（OpenAI）3 次全过，稳定写入 json_schema。

    Why 限流/超时只重试不计失败：RateLimitError / litellm.Timeout /
    asyncio.TimeoutError ≠ 不支持，只是"这次请求被网关挡了"或"网关慢/抖动"。
    限流/超时同属 transient infra 问题，sleep 后重试本次采样（指数退避
    5s→10s，最多 2 次整档共享，累计最多等 15s），直到返回非限流/非超时结果
    才判定该次采样。

    Why 基础设施错误单独分类：AuthenticationError（401）/ APIConnectionError
    （网络断）/ InternalServerError（500）/ ServiceUnavailableError（503）
    是临时性基础设施故障，不是"模型不支持 response_format"。如果归入
    model_rejected，两档失败 → prompt_only 写入配置 →
    _should_auto_probe_after_upgrade 永远 False → 首次升级启动时恰好
    API Key 失效/网关 500 的用户被永久静默降级，且永不重探。基础设施错误
    应该端点早返 probe_failed，不写配置，用户稍后手动重试。

    真实环境验证（2026-07-21）：
    - 豆包 Coding Plan：网关行为非确定性（flaky），同一请求 5 次采样 2 次
      schema 胜、3 次 prompt 胜。三次采样全过才升档，flaky 网关必然 ≥1 次
      静默忽略，稳定降级 prompt_only。
    - GLM：网关接受但模型输出漂移 → prompt_only
    - OpenAI：真正支持 → json_schema（3 次全过）

    约束：本端点独立于 /api/test-llm（启动器复用，禁止改动响应结构）。
    """

    from agent.generic.litellm_adapter import LiteLLMSession

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = {k.lower(): v for k, v in body.items()} if body else {}

    if body:
        # 用户传了 body（即使 apiKey 为空），用 body 测试——测试 case 传空 apiKey
        # 期望返回 probe_failed，不应回退读文件配置掩盖问题
        config = body
    else:
        from niu_api.llm_proxy import get_llm_config
        try:
            config = get_llm_config(use_lightrag_config=True)
        except Exception as e:
            return {"result": "probe_failed", "reason": f"读取配置失败: {e}", "mode": None, "raw_response": ""}
    config = {k.lower(): v for k, v in config.items()}

    # Ollama 等本地模型无需 API Key（apiBase 为 localhost/127.0.0.1 时豁免）
    apibase = config.get("apibase", "")
    is_local = apibase.startswith("http://localhost") or apibase.startswith("http://127.0.0.1") or apibase.startswith("https://localhost") or apibase.startswith("https://127.0.0.1")
    if not config.get("apikey") and not is_local:
        return {"result": "probe_failed", "reason": "API Key 未配置", "mode": None, "raw_response": ""}
    if not config.get("apibase"):
        return {"result": "probe_failed", "reason": "API 地址未配置", "mode": None, "raw_response": ""}
    if not config.get("model"):
        return {"result": "probe_failed", "reason": "模型名称未配置", "mode": None, "raw_response": ""}

    # 探测用 LiteLLMSession：复用运行时调用路径（含 drop_params=True 自动设置、
    # stream=True、temperature、provider_params 等），确保探测和运行时行为一致。
    # 关键：litellm_kwargs 必须含 allowed_openai_params=["response_format"]，
    # 否则 LiteLLM volcengine router 在客户端拒绝抛 UnsupportedParamsError，
    # 请求不会真正发到 provider 网关。
    # 同时 strip 掉 response_format_mode（项目自定义字段，非 OpenAI 标准），
    # 避免透传给 litellm.completion 触发 provider 4xx——虽然 LiteLLMSession
    # 在传 response_format 时强制 drop_params=True 会兜底丢弃，但不依赖隐性兜底。
    probe_litellm_kwargs = {
        k: v for k, v in (config.get("litellm_kwargs") or {}).items()
        if k != "response_format_mode"
    }
    probe_litellm_kwargs["allowed_openai_params"] = ["response_format"]
    probe_litellm_kwargs["max_tokens"] = 50

    base_llm_config = {
        "api_type": config.get("type", "openai"),
        "apikey": config["apikey"],
        "apibase": config["apibase"],
        "model": config["model"],
        # 探测与生产同参数（组件 3）：reasoning_effort 从配置透传（不再硬编码 None）——
        # chat() 内 assemble_request_params 注入 extra_body 送达
        "reasoning_effort": config.get("reasoning_effort"),
        "provider": config.get("provider", ""),
        # temperature 与运行时 _get_litellm_session 一致（默认 0.2），
        # 避免探测和运行时采样随机性差异
        "temperature": config.get("temperature", 0.2),
        "litellm_kwargs": probe_litellm_kwargs,
        # probe read_timeout 10s：豆包网关对 response_format 请求挂起时，10s 不响应基本就是挂起，
        # 快速失败降级，不等 60s。推理模型首响应慢的场景由外层 wait_for(90s) 兜底。
        "read_timeout": 10,
    }

    messages = _build_probe_messages()

    def _try_tier(response_format: dict | None) -> tuple[str, str]:
        """单次采样。返回 (tier_result, raw_text_or_reason)。

        判定逻辑：
        - 没抛异常 + 响应符合该档要求 → "supported"
        - 没抛异常 + 响应不符合 → "gateway_blocked"
        - 抛 RateLimitError → "rate_limited"（限流，不计失败，上层重试）
        - 抛 litellm.Timeout / openai.APITimeoutError → "timeout"（超时，不计失败，上层重试）
        - 抛 AuthenticationError / APIConnectionError / InternalServerError /
          ServiceUnavailableError → "infra_error"（基础设施错误，不写配置，端点早返 probe_failed）
        - 抛 BadRequestError / UnsupportedParamsError → "param_conflict"（错误文案含
          combination / reasoning_effort——参数组合无效（如推理深度档位与思考链状态不兼容），
          端点 probe_failed 阻断报"参数组合无效"，不降级不保存）
          或 "model_rejected"（其余 4xx 拒绝，该档失败降级）
        - 抛其他异常 → "model_rejected"（统一视为不支持，reason 记录供诊断）

        Why 限流/超时单独分类：RateLimitError / litellm.Timeout ≠ 不支持，只是
        "这次请求被网关挡了"或"网关慢/抖动"。限流/超时时上层
        _probe_tier_three_samples_async sleep 后重试本次采样，直到返回非限流/
        非超时结果才判定。

        Why 捕获 litellm.Timeout：慢厂商（本地 Ollama、DeepSeek 推理延迟）的
        真实超时路径是 litellm 在线程内 read_timeout（60s，覆盖推理模型
        首响应 20-120s 场景）先抛 litellm.Timeout（APITimeoutError 子类），
        外层 asyncio.wait_for（90s）几乎永远轮不到。
        如果不捕获，litellm.Timeout 会被 generic except Exception 归类
        model_rejected → 失败即停，慢但真支持的厂商被误杀。

        Why 基础设施错误单独分类：AuthenticationError（401）/ APIConnectionError
        （网络断）/ InternalServerError（500）/ ServiceUnavailableError（503）
        是临时性基础设施故障，不是"模型不支持 response_format"。如果归入
        model_rejected，两档失败 → prompt_only 写入配置 →
        _should_auto_probe_after_upgrade 永远 False → 首次升级启动时恰好
        API Key 失效/网关 500 的用户被永久静默降级，且永不重探。基础设施错误
        应该端点早返 probe_failed，不写配置，用户稍后手动重试。
        """
        import litellm
        import openai
        from litellm import (
            APIConnectionError,
            AuthenticationError,
            BadRequestError,
            InternalServerError,
            RateLimitError,
            ServiceUnavailableError,
            UnsupportedParamsError,
        )

        try:
            session = LiteLLMSession(cfg=base_llm_config)
            gen = session.chat(messages=messages, response_format=response_format)
            chunks = []
            mock_response = None
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                mock_response = e.value
            # stream_error 检查：流式错误 → infra_error（不写配置，端点早返 probe_failed）
            if mock_response and getattr(mock_response, 'stream_error', False):
                return "infra_error", f"stream_error: {getattr(mock_response, 'error_msg', '')[:150]}"
            text = mock_response.content if mock_response and hasattr(mock_response, 'content') and mock_response.content else "".join(chunks)
            if response_format is not None and response_format.get("type") == "json_schema":
                tier = _classify_probe_response_tier1(text)
            elif response_format is not None and response_format.get("type") == "json_object":
                tier = _classify_probe_response_tier2(text)
            else:
                tier = "gateway_blocked"
            return tier, text
        except RateLimitError as e:
            return "rate_limited", f"RateLimitError: {str(e)[:150]}"
        except (litellm.Timeout, openai.APITimeoutError) as e:
            return "timeout", f"{type(e).__name__}: {str(e)[:150]}"
        except (AuthenticationError, APIConnectionError, InternalServerError, ServiceUnavailableError) as e:
            return "infra_error", f"{type(e).__name__}: {str(e)[:150]}"
        except (BadRequestError, UnsupportedParamsError) as e:
            err_msg = str(e)
            reason = f"{type(e).__name__}: {err_msg[:150]}"
            # 参数组合 400（如 high + disabled）≠ response_format 不支持——必须报出阻断，
            # 不能误分类 model_rejected 降级 prompt_only（保存后入库仍 400）。
            # 按错误语义关键字区分（通用逻辑，不按 provider 特判）——litellm 1.88.1
            # openai 路由 400 分支 e.body=None，必须用 str(e)（message 恒含原始错误全文）。
            if "combination" in err_msg.lower() or "reasoning_effort" in err_msg.lower():
                return "param_conflict", reason
            return "model_rejected", reason
        except Exception as e:
            return "model_rejected", f"{type(e).__name__}: {str(e)[:150]}"

    # Tier 1: json_schema strict，三次采样
    tier1_result, tier1_raw = await _probe_tier_three_samples_async(
        lambda: asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_schema()),
            timeout=90,
        ),
        _build_probe_response_format_json_schema(),
    )

    if tier1_result == "rate_limited":
        return {
            "result": "probe_failed",
            "reason": "探测限流/超时重试 2 次仍未成功，请稍后手动重试",
            "mode": None,
            "raw_response": "",
        }

    if tier1_result == "infra_error":
        return {
            "result": "probe_failed",
            "reason": "探测遇到基础设施错误（API Key 失效/网络断/网关 5xx），请检查配置后手动重试",
            "mode": None,
            "raw_response": "",
        }

    # 参数组合 400（错误文案含 combination/reasoning_effort）→ 阻断报错，不降级不保存
    # （testAndSave lightrag 段组合测试：组合错误报真实错误，用户调整档位后重试）
    if tier1_result == "param_conflict":
        return {
            "result": "probe_failed",
            "reason": f"参数组合无效（模型拒绝）：{tier1_raw[:150]}。请调整思考链/推理深度档位（跟随模型默认或更低档位）后重试",
            "mode": None,
            "raw_response": "",
        }

    if tier1_result == "supported":
        return {
            "result": "supported",
            "mode": "json_schema",
            "reason": "Tier 1 三次采样全通过：模型+网关均稳定支持 json_schema strict 模式",
            "raw_response": "",
        }

    # Tier 2: json_object，三次采样
    tier2_result, tier2_raw = await _probe_tier_three_samples_async(
        lambda: asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_object()),
            timeout=90,
        ),
        _build_probe_response_format_json_object(),
    )

    if tier2_result == "rate_limited":
        return {
            "result": "probe_failed",
            "reason": "探测限流/超时重试 2 次仍未成功，请稍后手动重试",
            "mode": None,
            "raw_response": "",
        }

    if tier2_result == "infra_error":
        return {
            "result": "probe_failed",
            "reason": "探测遇到基础设施错误（API Key 失效/网络断/网关 5xx），请检查配置后手动重试",
            "mode": None,
            "raw_response": "",
        }

    # 参数组合 400（同 tier1）：阻断报错，不降级不保存
    if tier2_result == "param_conflict":
        return {
            "result": "probe_failed",
            "reason": f"参数组合无效（模型拒绝）：{tier2_raw[:150]}。请调整思考链/推理深度档位（跟随模型默认或更低档位）后重试",
            "mode": None,
            "raw_response": "",
        }

    if tier2_result == "supported":
        return {
            "result": "supported",
            "mode": "json_object",
            "reason": f"Tier 1 失败（{tier1_result}），Tier 2 三次采样全通过：模型支持 json_object 模式",
            "raw_response": "",
        }

    # Tier 3: 都失败，prompt_only 保底
    return {
        "result": "supported",
        "mode": "prompt_only",
        "reason": f"Tier 1（{tier1_result}: {tier1_raw[:60] if tier1_raw else ''}）+ Tier 2（{tier2_result}: {tier2_raw[:60] if tier2_raw else ''}）均失败，降级到 prompt-only 模式",
        "raw_response": "",
    }


@router.post("/api/model-capability-probe")
async def model_capability_probe(request: Request) -> dict:
    """探测模型能力（reasoning_effort/thinking/response_format/tools），写能力档案。

    settings 配置页"探测能力"按钮调用（llm 段与 lightrag 段各一个按钮）：
    body = llm 段或 lightrag 段配置（键名小写归一；顶层 `lightrag: true` 标记
    lightrag 场景——档案键后缀 |lightrag，竖线分隔与 model_probe.build_profile_key
    一致；llm/lightrag 段配置键名均不含 lightrag，无冲突）。

    调 niu_api/model_probe.probe 核心（与 CLI 共用同一实现，同步阻塞——≤11 次
    极小请求×单次 ≤10s ≈ 110s，值域候选超时重试最坏 7×2=14 次 ≈140s，放线程池
    避免阻塞事件循环），返回 probe_status
    JSON（含档案路径/键/摘要）。probe_status: ok / partial / failed
    （failed = 值域扫描遇非值域错误终止（超时重试 1 次后仍失败亦终止），不覆盖旧档案）。
    socket 超时对齐 test-connection 230s：探测全程预算最坏 ≈140s < 230s，无需外层
    额外超时；CLI 场景由主 Agent bash timeout=150/300 兜底。
    """
    from niu_api.model_probe import build_profile_key, default_profile_path, is_local_api_base, probe

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = {k.lower(): v for k, v in body.items()} if body else {}

    # lightrag 标记：body 顶层布尔（pop 后不进入探测 config）
    lightrag = bool(body.pop("lightrag", False))

    if not body.get("apibase"):
        return {"probe_status": "failed", "error": "API 地址未配置"}
    if not body.get("model"):
        return {"probe_status": "failed", "error": "模型名称未配置"}
    apikey = body.get("apikey", "")
    # 本地模型（localhost/127.0.0.1）免 apiKey——对齐 _probe_llm is_local 豁免
    if not apikey and not is_local_api_base(body.get("apibase", "")):
        return {"probe_status": "failed", "error": "API Key 未配置"}

    # user_config 按场景段包裹（_section_from_user_config 取段并小写归一）
    user_config = {"lightrag_llm": body} if lightrag else {"llm": body}
    try:
        profile = await asyncio.to_thread(
            probe,
            api_base=body["apibase"],
            api_key=apikey,
            model=body["model"],
            api_type=body.get("type", "openai"),
            lightrag=lightrag,
            user_config=user_config,
        )
        return {
            "probe_status": profile.get("probe_status", "failed"),
            "profile_path": str(default_profile_path()),
            "profile_key": build_profile_key(body["apibase"], body["model"], lightrag),
            "profile": profile,
        }
    except Exception as e:
        return {"probe_status": "failed", "error": f"探测异常: {e}"}

def _fetch_models_sync(api_base: str, api_key: str, api_type: str) -> tuple[int, bytes]:
    """同步拉取模型列表（urllib timeout=10s；经 asyncio.to_thread 调用，不阻塞事件循环）。

    URL 组装：openai → {apiBase.rstrip('/')}/models；anthropic → rstrip('/') 后不以 /v1
    结尾则补 /v1 再拼 /models（"/v1/" 结尾输入不先 rstrip 会拼出 /v1//v1/models），请求头
    x-api-key + anthropic-version: 2023-06-01（非 Authorization）。
    HTTPError（4xx/5xx）返回 (status, body)；其余网络异常（超时/拒绝/DNS）向上抛由调用方分类。
    """
    import urllib.error
    import urllib.request

    base = api_base.rstrip("/")
    if api_type == "anthropic":
        if not base.endswith("/v1"):
            base += "/v1"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    headers.setdefault("User-Agent", "Niu/0.3.2")  # Cloudflare 拦截 Python 默认 UA（403）
    req = urllib.request.Request(base + "/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _extract_context_window(item: dict):
    """单条模型条目提取上下文窗口：依次检查 context_length / max_input_tokens /
    context_window / top_provider.context_length（OpenRouter 嵌套形），首个非空整数胜出；无则 None。
    """
    candidates = [item.get("context_length"), item.get("max_input_tokens"), item.get("context_window")]
    top_provider = item.get("top_provider")
    if isinstance(top_provider, dict):
        candidates.append(top_provider.get("context_length"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


@router.post("/api/list-models")
async def list_models(request: Request) -> dict:
    """在线拉取模型列表（设置页「模型名称」下拉候选源）。

    纯转发探测，不落盘、不写配置。body = {apiKey, apiBase, type}（键小写归一，同
    model_capability_probe 先例）；本地模型（localhost/127.0.0.1）免 apiKey（复用
    is_local_api_base）。urllib 同步请求 timeout=10s 放 asyncio.to_thread。

    返回形状（前端状态机依赖；任何失败都不抛 500，全部分类为结构化 status）：
    - ok: {status, models:[{id, context_window?}], count}——OpenAI/Anthropic 均取 data[].id；
      窗口字段仅条目自带时携带（D5 零猜测）
    - unsupported: HTTP 404/405（网关不暴露 /models，如豆包 Plan 404）→ 前端降级手输不显示错误
    - error: 401/403（Key 无效）/超时/网络/5xx/解析失败 → 前端提示 reason 可重试
    """
    from niu_api.model_probe import is_local_api_base

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = {k.lower(): v for k, v in body.items()} if body else {}

    api_base_raw = body.get("apibase")
    api_base = api_base_raw.strip() if isinstance(api_base_raw, str) else ""
    if not api_base:
        return {"status": "error", "reason": "API 地址未配置"}
    api_key = body.get("apikey") or ""
    # 本地模型（localhost/127.0.0.1）免 apiKey——同 model_capability_probe 先例
    if not api_key and not is_local_api_base(api_base):
        return {"status": "error", "reason": "API Key 未配置"}

    api_type_raw = body.get("type")
    api_type = api_type_raw.lower() if isinstance(api_type_raw, str) else "openai"
    try:
        http_status, payload = await asyncio.to_thread(_fetch_models_sync, api_base, api_key, api_type)
    except Exception as e:
        return {"status": "error", "reason": f"获取模型列表失败（网络/超时）: {e}"}

    if http_status in (404, 405):
        return {"status": "unsupported", "reason": "网关不支持模型列表接口"}
    if http_status in (401, 403):
        return {"status": "error", "reason": "API Key 无效或无权访问模型列表"}
    if http_status >= 400:
        return {"status": "error", "reason": f"网关返回 HTTP {http_status}，请稍后重试"}

    try:
        data = json.loads(payload)
    except Exception:
        return {"status": "error", "reason": "模型列表响应解析失败"}
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return {"status": "error", "reason": "模型列表响应格式非预期（缺少 data 数组）"}

    models = []
    for item in data["data"]:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        entry = {"id": mid}
        window = _extract_context_window(item)
        if window is not None:
            entry["context_window"] = window
        models.append(entry)

    return {"status": "ok", "models": models, "count": len(models)}


@router.get("/api/preload-status")
async def get_preload_status():
    """Get preload status - used by launcher to wait before showing window"""
    return {
        "ready": _preload_complete,
        "uptime": str(datetime.now() - _startup_time).split(".")[0],
        "stage": _preload_stage,
    }


def _count_entities_from_graph(adapter) -> tuple[int, int]:
    """Fallback: count persons and notes by traversing NetworkX graph.

    Used when _entity_type_counts cache is empty (before first refresh).
    """
    persons = 0
    notes = 0
    try:
        rag = adapter._get_rag()
        if rag is not None:
            graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
            if graph_obj is not None:
                nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                if nx_graph is not None:
                    from niu_api.internal.lightrag_manager import graph_read_lock

                    with graph_read_lock():
                        snapshot = nx_graph.copy()

                    for node_name in snapshot.nodes():
                        attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                        entity_type = attrs.get("entity_type", "").lower()
                        if entity_type == "person":
                            persons += 1
                        elif entity_type in ("note", "knowledge"):
                            notes += 1
    except Exception:
        pass
    return persons, notes


@router.get("/api/stats")
async def get_stats(agent: str | None = None) -> StatsResponse:
    """Get system stats"""
    store = await get_message_store()
    messages = await store.count_messages()
    uptime = str(datetime.now() - _startup_time).split(".")[0]

    files = 0
    persons = 0
    notes = 0

    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()

        # Document count: processed documents from LightRAG
        doc_status = adapter.document_status()
        if isinstance(doc_status, dict) and "processed" in doc_status:
            files = doc_status["processed"]

        # Person and note counts: from cached entity type counts (O(1))
        try:
            from agent.brain_tools import get_activation_mgr

            activation_mgr = get_activation_mgr()
            if activation_mgr is not None:
                type_counts = activation_mgr.get_entity_type_counts()
                if type_counts:
                    persons = type_counts.get("person", 0)
                    notes = type_counts.get("note", 0) + type_counts.get("knowledge", 0)
                else:
                    # Cache empty (not yet refreshed), fall back to graph traversal
                    persons, notes = _count_entities_from_graph(adapter)
            else:
                persons, notes = _count_entities_from_graph(adapter)
        except Exception:
            persons, notes = _count_entities_from_graph(adapter)
    except Exception as e:
        logger.debug(f"[Stats] LightRAG stats unavailable: {e}")

    # 计算上下文使用率（优先用 LLM API 返回的真实 prompt_tokens）
    context_usage = 0.0
    context_cache_hit = None
    try:
        context_window = _read_context_window_tokens()
        real_tokens = 0
        cached_tokens = None
        if agent:
            # 子 Agent：从 SubagentRegistry 读运行中 handler 的真实 prompt_tokens
            try:
                from agent.subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(agent)
                if instance is not None:
                    handler_ref = getattr(instance, "handler", None) or getattr(instance, "suspended_handler", None)
                    real_tokens = getattr(handler_ref, "_last_prompt_tokens", 0) or 0
                    cached_tokens = getattr(handler_ref, "_last_cached_tokens", None)
            except Exception:
                pass
        else:
            try:
                from niu_api.chat import get_or_create_runner
                runner = get_or_create_runner()
                real_tokens = getattr(getattr(runner, 'handler', None), '_last_prompt_tokens', 0) or 0
                cached_tokens = getattr(getattr(runner, 'handler', None), '_last_cached_tokens', None)
            except Exception:
                pass
        if real_tokens > 0:
            context_usage = real_tokens / context_window if context_window > 0 else 0.0
            # 语义：None=服务端未返回（未知）；0=真实零命中——两者如实上报
            if cached_tokens is not None:
                context_cache_hit = min(1.0, cached_tokens / real_tokens)
        elif not agent:
            # 主 Agent 无真实 tokens 时 fallback 估算全库消息；子 Agent 无此概念，直接 0
            context_usage = (await compute_context_usage_estimate(store=store, context_window=context_window)) or 0.0
    except Exception:
        context_usage = 0.0

    return StatsResponse(messages=messages, uptime=uptime, files=files, persons=persons, notes=notes,
                         context_usage=context_usage, context_cache_hit=context_cache_hit)


def _force_exit_after_delay():
    """3秒后强制退出进程，确保 /api/shutdown 不会让进程变成僵尸"""
    time.sleep(3)
    os._exit(0)


@router.post("/api/shutdown")
async def shutdown():
    """Shutdown the server gracefully"""
    logger.info("Shutdown requested via API")

    # Wait for pending LightRAG fire-and-forget futures (entity extraction)
    # Go launcher has 2s HTTP timeout, so we must complete within that window
    # shutdown_pending_futures is blocking (uses future.result with timeout),
    # so we run it in a thread to avoid blocking the asyncio event loop.
    from niu_api.internal.lightrag_manager import shutdown_pending_futures
    await asyncio.to_thread(shutdown_pending_futures, timeout=1.5)

    # 启动兜底退出线程：3秒后强制 os._exit(0)，
    # 给 Go 启动器足够时间收到 HTTP 响应后再终止进程
    threading.Thread(target=_force_exit_after_delay, daemon=True).start()

    logger.info("Python API ready for shutdown")
    return {"status": "shutting down"}


@router.post("/api/chat/session")
async def chat_session(request: ChatRequest) -> ChatResponse:
    """
    Chat endpoint - uses GenericAgentRunner with original GenericAgent code

    Uses runner.py which correctly imports from agent/generic/
    """
    from niu_api.config import get_config

    config = get_config()

    if not config.llm or not config.llm.api_key:
        return ChatResponse(reply="Error: LLM not configured, please set API Key first")

    # Case 3 唤醒接线（§3.4）：electron 用户动作打断睡眠管道（幂等 sleep→idle 才翻转语义由
    # set_spirit_state 归一化保证）。source==""（异步子 Agent 回填程序化流量）不唤醒。
    # R5 枚举闭合：chat_session 的 source 仅 {'electron', ''} 两值，判据闭合。
    # 门控放在函数入口：下方 supplement 提前返回路径（_chat_lock.locked() 分支）同样被覆盖，
    # 无需重复接线；ask_user 回答注入路径按方案定案不接唤醒（等待期持锁+忙碌守卫使
    # "睡眠中收到 ask_user 回答"基本不可达，此处误唤醒无害）。
    if request.source == "electron":
        set_spirit_state("idle")

    # --- /stop directive: stop current Agent work ---
    if request.message.strip() == "/stop":
        from agent.runner import request_stop
        request_stop()
        logger.info("[ChatSession] /stop requested")
        return ChatResponse(reply="已停止")

    # --- 见缝插针：Agent 运行期间，将补充消息入队并立即返回 ---
    if _chat_lock.locked():
        # 用户回答主 Agent ask_user 提问：直接注入 set_answer（不走补充队列）
        # ——回答随下一轮 [user 回答] 返回 do_ask_user；消息以 user 角色持久化 + SSE 推送（前端可见）
        # 只 guard import 语句：set_answer 后段错误不得被吞（吞了会落到补充队列→重复投递）
        # set_answer 返回值判定（无注册 future 返回 False → 落补充队列），消除 is_waiting TOCTOU 双回答竞态
        try:
            from agent.ask_user import get_user_ask_registry
        except ImportError:
            pass
        else:
            if get_user_ask_registry().set_answer("main-agent", request.message):
                logger.info(f"[chat_session] ask_user answered: {request.message[:50]}...")
                store = await get_message_store()
                user_msg_id = await store.add_message(role="user", content=request.message)
                from niu_api.chat import notify_new_message
                await notify_new_message(user_msg_id, "user", request.message, source="electron")
                return ChatResponse(reply="已收到", session_id="default", message_id=user_msg_id)
        # 原有补充队列逻辑（保留）
        from agent.runner import enqueue_supplement

        # 持久化 user 消息（与正常路径一致）
        store = await get_message_store()
        user_msg_id = await store.add_message(role="user", content=request.message)

        # SSE 推送 user 消息给前端
        from niu_api.chat import notify_new_message
        await notify_new_message(user_msg_id, "user", request.message, source="electron")

        # 入队补充消息，立即返回
        enqueue_supplement(request.message)
        logger.info(f"[chat_session] Supplement enqueued: {request.message[:50]}...")
        return ChatResponse(reply="已收到", session_id="default", message_id=user_msg_id)

    # 锁未被占用：正常获取锁并处理
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=600.0)
    except TimeoutError:
        logger.warning("[chat_session] _chat_lock 600s timeout, request rejected")
        return ChatResponse(reply="系统正忙，请稍后再试", session_id="default")

    try:
        # Get message store
        store = await get_message_store()

        # Store user message
        user_msg_id = await store.add_message(role="user", content=request.message)
        # 通知 SSE 推送用户消息（前端用此 ID 给本地渲染的 user 气泡补上 data-id）
        from niu_api.chat import notify_new_message
        await notify_new_message(user_msg_id, "user", request.message, source="electron")

        # P1-1: 使用 ContextManager 加载历史（统一管理）
        from agent.context_manager import get_context_manager

        context_manager = await get_context_manager(store)
        history_for_runner = await context_manager.get_context_for_chat(exclude_last=True)
        history_len = len(history_for_runner)

        logger.info(f"Loaded {len(history_for_runner)} history messages")

        # Get runner (uses original GenericAgent from agent/generic/)
        # Use the pre-initialized runner from niu_api/chat.py which has MCP tools
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()

        # Create a simple session_id (no session concept, but runner needs one)
        session_id = "default"

        # 每次用户发起新对话时，清除停止标志
        from agent.runner import clear_stop, drain_supplements
        clear_stop()
        # 清理残留的补充消息（这些消息已被持久化，会通过历史加载重新进入上下文）
        drain_supplements()
        # 通道继承：Electron 用户消息清除 IM 通道；子 Agent 注入（source=""）继承
        if request.source == "electron":
            runner.set_im_channel("")
            runner.set_im_force(False)  # Electron 用户消息转假（规则 2 + 粘性清除）

        # Run chat using asyncio.to_thread to avoid blocking event loop
        def sync_chat():
            chunks = []
            for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner, resources=request.resources or None, channel_id=runner.get_im_channel()):
                chunks.append(chunk)
            return "".join(chunks)

        chat_error = None
        try:
            full_reply = await asyncio.to_thread(sync_chat)
        except Exception as e:
            import traceback
            logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
            chat_error = e  # E2：保留异常对象（str() 化后 type() 判定恒 'str'——is_litellm_error_type 失效）；str() 插值/None 判定/日志均兼容
            full_reply = f"Error: {str(e)}"

        # 方案 A：异常时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
        if chat_error is None:
            rv = getattr(runner, "last_return_value", None)
            if rv and isinstance(rv, dict) and rv.get("result") == "LLM_ERROR":
                # E2：LLM_ERROR 错误文本不落库（用户拍板"不写 DB"——刷新 Chat 从 DB 加载历史时自然消失）
                message_id = None  # 显式初始化：返回处无条件读取，不初始化则 NameError 500
                error_msg = rv.get("error_msg", "") or ""
                error_type = rv.get("error_type")
                from niu_api.chat import notify_llm_error_sync
                from agent.generic.litellm_adapter import extract_error_type, format_llm_error_for_user
                notify_llm_error_sync(
                    error_type or extract_error_type(error_msg),
                    format_llm_error_for_user(error_msg, error_type),
                    "chat_session",
                )
                # skip persist（错误文本不落库）
            else:
                # 双管道持久化：使用 persist_agent_reply 统一处理
                from niu_api.chat import persist_agent_reply
                persisted_msgs = getattr(runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
                extracted_at_msgs = getattr(runner, "_extracted_at_msgs", None)  # 修正版方案：轮中提取的 subagent_msg（去重用）
                message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs, extracted_at_msgs=extracted_at_msgs)
        else:
            rv = getattr(runner, "last_return_value", None)
            message_id = None
            logger.warning(f"[Chat Session] Skipped persist due to chat error: {chat_error}")
            # E2：LLM 异常（异常穿透路径——缺口①，错误 key/模型不存在等主场景）→ 友好文案 + notify
            from niu_api.chat import notify_llm_error_sync
            from agent.generic.litellm_adapter import format_llm_error_for_user, is_litellm_error_type
            type_name = type(chat_error).__name__ if isinstance(chat_error, BaseException) else None
            if type_name and is_litellm_error_type(type_name):
                full_reply = format_llm_error_for_user(str(chat_error), type_name)
                notify_llm_error_sync(type_name, full_reply, "chat_session")
            # 非 LLM 异常（内部 bug）不 notify、full_reply 保持既有（不误标"模型调用失败"）

        # 检测主 Agent 上下文溢出 → fire-and-forget 机械压实（Task 3 收编，替代 force 投递）
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat Session] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"running mechanical compaction (fire-and-forget)"
            )
            from niu_api.chat import fire_and_forget_compaction
            fire_and_forget_compaction(store, source="ChatSession")
    finally:
        from agent.runner import clear_stop, drain_supplements
        clear_stop()  # 防御性清除：确保停止标志不残留
        drain_supplements()  # 清理残留补充消息，防止被 ChatQueue 路径读取
        _chat_lock.release()

    # IM 推送在锁释放后执行（与 ChatQueue 模式一致，避免网络 I/O 阻塞锁）
    # 统一入口 push_im_reply：should_push_im 闸门在函数内；channel_id 非空 → route_out(SEND)；
    # force-only（channel_id 空）→ send_sync("") SEND 终结流式卡片（修复 08-12 只在 ChatQueue
    # 分支 2 修终结、chat_session 缺口导致的卡片"思考中"不终结——2026-08-17）。
    # chat_error 也投递：错误文案流式期已进卡（LLM 错误）必须终结；对齐 ChatQueue 分支 2 语义。
    # 规则 5：此处只读标志，绝不 set_im_channel / set_im_force——子 Agent 返回不改变标志。
    try:
        from niu_api.channel.gateway import push_im_reply
        await push_im_reply(runner, full_reply)
    except Exception as e:
        logger.warning(f"[chat_session] IM push failed: {e}")

    return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)


@router.get("/api/context/messages")
async def get_context_messages(
    limit: int = 100, before_id: str | None = None, full: bool = False, session_id: str | None = None
) -> MessagesResponse:
    """Get messages

    Args:
        limit: Number of messages to return (default 100)
        before_id: Get messages before this ID (for pagination)
        full: If True, return full content
        session_id: Ignored (kept for compatibility with session-manager)
    """
    store = await get_message_store()
    messages = await store.get_messages(limit, before_id)
    total = await store.count_messages()

    return MessagesResponse(
        messages=[
            MessageResponse(
                id=msg.id, role=msg.role, content=msg.content, created_at=msg.created_at
            )
            for msg in messages
            if msg.role != "tool" and not (msg.role == "assistant" and not (msg.content or "").strip() and msg.tool_calls)  # 过滤 tool 消息 + 空 content 带 tool_calls 的 assistant 消息
        ],
        total_in_db=total,
    )


@router.post("/api/context/messages/delete")
async def delete_context_messages(request: dict) -> dict:
    """Delete messages by IDs

    Args:
        request: {
            "session_id": str (ignored),
            "message_ids": [str],
            "reason": str (optional)
        }

    Returns:
        {
            "deleted_count": int,
            "freed_tokens": int
        }
    """
    message_ids = request.get("message_ids", [])
    reason = request.get("reason", "Context compression")

    if not message_ids:
        return {"deleted_count": 0, "freed_tokens": 0}

    logger.info(f"[Context] Deleting {len(message_ids)} messages, reason: {reason}")

    store = await get_message_store()
    result = await store.delete_messages_by_ids(message_ids)
    logger.info(f"[Context] Deleted {result['deleted_count']} messages, freed {result['freed_tokens']} tokens")

    return result


@router.post("/api/context/messages/update")
async def update_context_message(request: dict) -> dict:
    """Update message content by ID.

    Args:
        request: {
            "session_id": str (ignored),
            "message_id": str,
            "content": str
        }

    Returns:
        {"status": "ok"} or {"status": "error", "message": str}
    """
    message_id = request.get("message_id")
    content = request.get("content")

    if not message_id or not content:
        return {"status": "error", "message": "message_id and content are required"}

    store = await get_message_store()
    updated = await store.update_message(message_id, content)

    if updated:
        logger.info(f"[Context] Updated message id={message_id}")
        return {"status": "ok"}
    return {"status": "error", "message": f"Message {message_id} not found"}


@router.post("/api/context/messages/add")
async def add_context_message(request: dict) -> dict:
    """Add a message to the session.

    Args:
        request: {
            "session_id": str (ignored),
            "role": str,
            "content": str
        }

    Returns:
        {"status": "ok", "message_id": str}
    """
    role = request.get("role")
    content = request.get("content")

    if not role or not content:
        return {"status": "error", "message": "role and content are required"}

    store = await get_message_store()
    msg_id = await store.add_message(role=role, content=content)

    return {"status": "ok", "message_id": msg_id}

def _reset_runner_brain_state(runner) -> None:
    """会话边界清空脑区注入缓存（防跨会话旧实体注入）。

    激活管理器为跨会话单例 + _recent_region_entities 缓存无会话生命周期——
    /new 与 /clear 不清缓存会使新会话前 ~11-15 轮持续注入上一会话缓存实体。
    用 getattr 直接读属性而非 _get_brain_injector()（None 时后者会触发 LightRAG
    懒初始化 300s 阻塞 + forced-sync daemon spawn——clear 路径不应有此副作用；
    该场景缓存恒空，clear 本就是 no-op）。
    """
    inj = getattr(runner, "_brain_injector", None)
    if inj is None:
        return
    try:  # 防注入器创建路径异常传播破坏 clear
        inj.clear_recent_region_entities()
    except Exception as e:
        logger.warning(f"Clear brain region cache failed: {e}")


@router.post("/api/chat/clear")
async def clear_chat(request: Request) -> dict:
    """Clear all messages (for /new and /clear commands)

    即时清除语义（§3.6）：取消清空前提炼——不再读取任何提炼开关字段，原「清空前先跑
    force 整理」通道已整块删除。
    """
    # ① 停止主 Agent（既有）；② 唤醒睡眠管道（新增，无条件）——用户动作打断 Case 3
    from agent.runner import clear_stop, request_stop
    request_stop()
    set_spirit_state("idle")

    # ③ 排队拿锁：无限心跳等待（替代旧 120s 拒绝清除）——对在途对话请求仍是排队语义
    if not await _acquire_chat_lock_with_retry("ClearChat"):
        # 防御分支（生产不可达，仅测试注入 max_elapsed）
        logger.error("[ClearChat] lock wait aborted, clear rejected")
        clear_stop()  # 防止停止标志残留，影响后续定时任务
        return {"success": False, "error": "系统正忙，请稍后再试"}

    try:
        clear_stop()  # 防御性清除：确保清空时标志干净
        # 清理残留的补充消息
        from agent.runner import drain_supplements
        drain_supplements()
        store = await get_message_store()
        count = await store.clear_messages()

        # 重置 runner 的所有状态
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if runner:
            # 重置 handler 的工作记忆
            if runner.handler:
                runner.handler.reset_working_memory()
                runner.handler._last_prompt_tokens = 0
                runner.handler._last_cached_tokens = None
            # 清空衰减池（新会话开始）
            runner._decay_pool.clear()
            # 清空脑区注入缓存（_recent_region_entities——防跨会话旧实体注入）
            _reset_runner_brain_state(runner)

            # Note: LLM session history is managed by ContextManager,
            # which reloads from message store each call.
            # store.clear_messages() above already clears persistent history.

        # 清空临时目录（画框图片等）
        from agent.tmp_dir import cleanup_all_tmp
        cleaned_tmp = cleanup_all_tmp()

        from agent.md_mirror import truncate_relay_files
        truncate_relay_files()
        logger.info("[Clear] F1/F2 relay files truncated")

        # Task 8：/new 清理面——指针块删除 + 校准倍率复位 + 内存派生状态作废
        # （F1/F2/F3 上面已截断；journal.md 本体按 §8 拍板保留）
        from agent.context_assembler import reset_derived_state
        reset_derived_state()
        # 清理挂起同步子 Agent session（清空会话 = 显式放弃当前全部工作，与 reset_derived_state 同语义）
        from agent.runner import cleanup_suspended_sync_subagents
        cleanup_suspended_sync_subagents({"result": "STOPPED"})

        return {"success": True, "deleted_count": count, "cleaned_tmp": cleaned_tmp}
    finally:
        _chat_lock.release()


@router.get("/api/pending-alerts")
async def get_pending_alerts() -> list:
    """Get pending alerts - delegates to alerts module"""
    from niu_api.alerts import get_and_clear_pending_alerts
    return get_and_clear_pending_alerts()


@router.post("/api/spirit-state")
async def set_spirit_state_endpoint(request: dict):
    """精灵状态通道：Electron 主进程（main.js spirit-state IPC）转发状态转换。

    body: {"state": str}——"sleep" 表示睡眠，其余值（"idle" 等）表示非睡眠。
    """
    state = request.get("state", "") if isinstance(request, dict) else ""
    set_spirit_state(state)
    return {"status": "ok", "state": _SPIRIT_STATE}


@router.post("/api/context/tidy")
async def tidy_context(request: dict):
    """
    Tidy context when entering sleep mode or forced compression

    Args:
        request: {
            "session_id": str,
            "mode": "sleep" | "compact"
        }

    Returns:
        {
            "status": "success",
            "message": str,
            "freed_tokens": int (optional)
        }
    """
    # 挂点 2（冗余）：睡眠整理触发时冗余置位（主挂点为 main.js spirit-state 转发）
    if (request.get("mode") or "").lower() == "sleep":
        set_spirit_state("sleep")
    mode = (request.get("mode") or "sleep").lower()
    if mode == "compact":
        # /compact 重定义（D12）：手动触发批量压实，与自动触发共用同一压实函数；
        # 纯机械秒级、零 LLM、无 ChatQueue pause 门禁——直接执行不经整理队列
        return await _compact_context_impl(request)
    if mode != "sleep":
        return {"status": "error", "message": f"Unknown mode: {mode}. Use 'sleep' or 'compact'."}
    # 全局整理队列投递：sleep → 投递 + 立即返回 queued（result 无人消费，CP0 cancelled 依赖此）。
    # None 窗口防御（§3.0 Option A）：队列未创建时同步执行，调用方等完成。
    if _pipeline_queue is None:
        return await _tidy_context_impl(request)
    fut = _pipeline_enqueue(mode, request, held=False)
    return {"status": "queued"}


async def _compact_context_impl(request: dict) -> dict:
    """/compact 直达实现：调 compaction.compact_now_detailed 并回传圆环 usage。"""
    session_id = (request or {}).get("session_id", "default")
    from niu_api.chat import notify_compact_status_sync
    try:
        notify_compact_status_sync("started", mode="compact")
    except Exception:
        pass
    try:
        store = await get_message_store()
        from agent.context_assembler import compaction
        _, stats = await compaction.compact_now_detailed(store)
        logger.info(f"[Compact] manual compact done: session={session_id}, "
                    f"keep_turns={stats['keep_turns']}, blocks_archived={stats['blocks_archived']}, "
                    f"tools_placeholderized={stats['tools_placeholderized']}, "
                    f"est_usage={stats.get('usage')}")
        try:
            notify_compact_status_sync("done", mode="compact",
                                       usage=stats.get("usage"), reset_tokens=True)
        except Exception:
            pass
        return {
            "status": "ok",
            "mode": "compact",
            "tokens_estimate": stats["tokens_estimate"],
            "context_window": stats["context_window"],
            "usage": stats.get("usage"),
        }
    except Exception as e:
        logger.error(f"[Compact] manual compact failed: {e}")
        try:
            notify_compact_status_sync("done", mode="compact")
        except Exception:
            pass
        return {"status": "error", "mode": "compact", "message": str(e)}


async def _tidy_context_impl(request: dict, chat_lock_already_held: bool = False):
    """tidy_context 的内部实现（不加锁，由调用方负责并发控制）。

    Args:
        chat_lock_already_held: 调用方已持有 _chat_lock 时传 True（None 窗口同步路径），
            跳过重复获取 _chat_lock（asyncio.Lock 不可重入）。
    """
    # 压缩前状态占位：finally 判定是否实际压缩（try 早期失败/取消时保持默认 -1/None）
    _msgs_before = -1  # 压缩前消息数（finally 判定是否实际压缩；-1 表示 try 早期未读到）
    _store_ref = None  # try 内 store 引用（finally 复用，避免二次 get_message_store）
    # Task 6：压缩退役后本实现仅服务 mode="sleep"（journal/entity/dream/摘要）；
    # ChatQueue pause 门禁与 skip_compress 键随 force 投递面一同退役
    session_id = request.get("session_id", "default")
    mode = request.get("mode", "sleep")
    # 广播压缩状态 started 事件（前端圆环动画启动）
    try:
        from niu_api.chat import notify_compact_status_sync
        notify_compact_status_sync("started", mode=mode)
    except Exception:
        pass

    logger.info(f"[Tidy] Context tidy triggered: session={session_id}, mode={mode}")

    try:
        # Get message store
        store = await get_message_store()
        messages = await store.get_messages()

        # 记录压缩前状态（finally 判定是否实际压缩：消息数减少 = 删过消息）
        _store_ref = store
        _msgs_before = len(messages)

        if not messages:
            logger.info("[Tidy] No messages to tidy")
            return {"status": "success", "message": "No messages to tidy"}

        # Calculate per-message token counts
        message_count = len(messages)
        msg_tokens = []
        try:
            from agent.token_calculator import TokenCalculator
            calc = TokenCalculator.get()
            for msg in messages:
                try:
                    t = calc.count_message_single(msg.role, msg.content or "", tool_calls=msg.tool_calls)
                except Exception:
                    t = max(1, len(msg.content or "") // 2) + 4
                msg_tokens.append(t)
        except ImportError:
            msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]
        estimated_tokens = sum(msg_tokens)

        # 读取上下文窗口大小（tokens）
        context_window_tokens = _read_context_window_tokens()

        # 优先用 LLM API 返回的真实 prompt_tokens 计算 usage_percent
        real_prompt_tokens = 0
        try:
            from niu_api.chat import get_or_create_runner
            _runner = get_or_create_runner()
            real_prompt_tokens = getattr(getattr(_runner, 'handler', None), '_last_prompt_tokens', 0) or 0
        except Exception:
            pass
        if real_prompt_tokens > 0:
            usage_percent = (real_prompt_tokens / context_window_tokens) * 100 if context_window_tokens > 0 else 0
            display_tokens = real_prompt_tokens
            logger.info(f"[Tidy] Current context: {message_count} messages, real_tokens={real_prompt_tokens}, est_tokens={estimated_tokens}, {usage_percent:.1f}%")
        else:
            usage_percent = (estimated_tokens / context_window_tokens) * 100 if context_window_tokens > 0 else 0
            display_tokens = estimated_tokens
            logger.info(f"[Tidy] Current context: {message_count} messages, {estimated_tokens} tokens, {usage_percent:.1f}%")

        from agent.runner import clear_stop, is_stop_requested

        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if not runner:
            logger.warning("[Tidy] Runner not initialized")
            return {"status": "error", "message": "Runner not initialized"}

        if mode == "sleep":
            # Sleep mode: entity-extractor (F1 自读) → dream-evolver (F3 自读多轮循环) → 块摘要（可选层）
            # （journal 腿已迁 scheduler journal_daily 定时任务——直执行分支自管游标，不经本管道）

            llm_config = runner.llm_config

            # 1/3. entity-extractor（v2：自读 F1 → processed_line → relay 剪切）
            from agent.md_mirror import F1_PATH

            from niu_api.md_alignment import align_f1_with_store

            f1_path = F1_PATH
            f1_nonempty = os.path.exists(f1_path) and os.path.getsize(f1_path) > 0

            if not f1_nonempty:
                logger.info("[Tidy] entity-extractor: F1 空/不存在，跳过提炼")
            else:
                try:
                    patched = await align_f1_with_store(store, f1_path)
                    if patched:
                        logger.info(f"[Tidy] F1 aligned: +{patched} records")
                except Exception as e:
                    logger.warning(f"[Tidy] F1 对齐失败（跳过）: {e}")

                def run_entity_extractor():
                    return _call_entity_extractor_on_f1(llm_config, f1_path)

                entity_result = await asyncio.to_thread(run_entity_extractor)
                logger.info(f"[Tidy] entity-extractor result: {entity_result[:200]}")
                if _is_subagent_overflow(entity_result) or _is_subagent_incomplete(entity_result) or _is_subagent_failure(entity_result):
                    logger.warning("[Tidy] entity-extractor 未正常完成 — F1 不剪切，下次重跑")
                else:
                    cut = _parse_and_relay_f1(entity_result, f1_path)
                    logger.info(f"[Tidy] relay cut {cut} lines" if cut else "[Tidy] relay skipped (invalid line number)")

            # CP1：entity 段完成后——非睡眠 → 中断；F1 剪切已执行不回滚，下次续跑
            if not is_sleeping():
                logger.warning("[Tidy] Sleep interrupted after entity-extractor (woke up)")
                return {"status": "interrupted", "reason": "woke_up"}

            # 2/3. dream-evolver（v3 多轮子循环：F2 头部重建 F3 工作集 → 自读报行号 → 删 F2 前缀；
            # 终止判据 covered_all=本轮 F3 涵盖全量 F2，非「F2 删空」——会话边界合法留置尾部时 F2 永不删空）
            from agent.md_mirror import F2_PATH, build_f3_from_f2, drop_f2_prefix

            def _f2_line_count() -> int:
                """F2 当前行数（与 md_mirror 同法剥离 split 尾部伪影，使边界值==显示行数）。"""
                with open(F2_PATH, encoding="utf-8") as f:
                    _lines = f.read().split("\n")
                if _lines and _lines[-1] == "":
                    _lines.pop()
                return len(_lines)

            while True:
                f2_total = _f2_line_count() if os.path.exists(F2_PATH) else 0
                if f2_total == 0:
                    break  # D5：F2 空/不存在 → 无梦境待处理
                f3_lines = await asyncio.to_thread(build_f3_from_f2)
                if f3_lines == 0:
                    logger.error(f"[Tidy] dream-evolver: F2 有内容（{f2_total} 行）但 build_f3 返回 0（畸形停摆），本轮放弃")
                    break
                covered_all = f3_lines >= f2_total  # ★ 本轮 F3 是否涵盖全量 F2
                dream_result = await asyncio.to_thread(_call_dream_evolver_on_f3, llm_config)
                logger.info(f"[Tidy] dream-evolver result: {dream_result[:200]}")
                # dream-evolver 游标已退役（工程五七件套）：只删 F2 前缀，无任何游标读写
                if _is_subagent_failure(dream_result) or _is_subagent_incomplete(dream_result):
                    if _is_subagent_incomplete(dream_result):
                        logger.warning(f"[Tidy] dream-evolver incomplete ({_incomplete_reason(dream_result)}) — F2 不动，下次重跑")
                    else:
                        logger.warning(f"[Tidy] dream-evolver failure: {dream_result[:200]} — F2 不动，下次重跑")
                    break
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(
                        f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns — "
                        f"drop 前 ⌊{f3_lines}/3⌋ 行部分进度后中断"
                    )
                    deleted_lines, _dropped_msg_id = await asyncio.to_thread(
                        drop_f2_prefix, max(1, f3_lines // 3), f3_lines
                    )
                    if deleted_lines:
                        logger.info(f"[Tidy] dream-evolver 本轮完成：overflow 部分进度删前 {deleted_lines} 行")
                    break
                deleted_lines, _done_msg_id = await asyncio.to_thread(_parse_and_drop_f2, dream_result, f3_lines)
                if deleted_lines:
                    logger.info(f"[Tidy] dream-evolver 本轮完成：删 F2 前 {deleted_lines}/{f2_total} 行")
                else:
                    logger.warning("[Tidy] dream-evolver 未输出有效 processed_line 或删除无进度 — F2 不动")
                if deleted_lines == 0 and not covered_all:
                    break  # 零进度防空转（M 无效/校验失败分支）
                if not is_sleeping():
                    logger.warning("[Tidy] Sleep interrupted in dream-evolver loop (woke up)")
                    return {"status": "interrupted", "reason": "woke_up"}
                if covered_all:
                    break  # ★ 终止判据：本轮 F3 已涵盖全量 F2

            # 补全会话日期链（循环退出后一次收尾全覆盖；幂等图补边扫描，方法内部已容错）
            await asyncio.to_thread(runner._ensure_session_chain)

            # CP2：dream 循环完成后——纯中断检查；F2 前缀删除已执行不回滚，下次续跑
            if not is_sleeping():
                logger.warning("[Tidy] Sleep interrupted after dream-evolver (woke up)")
                return {"status": "interrupted", "reason": "woke_up"}

            return {"status": "ok", "mode": "sleep", "tokens_before": display_tokens}

        else:
            logger.warning(f"[Tidy] Unknown mode: {mode}, skipping")
            return {"status": "error", "message": f"Unknown mode: {mode}. Use 'sleep' or 'compact'."}

    except Exception as e:
        import traceback
        logger.error(f"[Tidy] Error: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e)}
    finally:
        # 无论成功/失败/异常/任务取消都必须广播 done，避免前端圆环卡死：
        # 1) 先无条件保底广播（无 await——asyncio.CancelledError 继承 BaseException 不入
        #    except Exception，取消时 finally 首个 await 点会抛，保底广播必须在 await 之前完成）
        # 2) 再 await 重算：仅"实际压缩"（消息数减少）补推 usage + reset_tokens（二次 done 幂等，
        #    前端先 loadStats 兜底后 render usage，二者同源一致）
        try:
            from niu_api.chat import notify_compact_status_sync
            notify_compact_status_sync("done", mode=mode)
            try:
                _compressed, usage_after = await _compute_post_compress_usage(
                    store=_store_ref, msgs_before=_msgs_before)
                if _compressed:
                    notify_compact_status_sync("done", mode=mode, usage=usage_after, reset_tokens=True)
            except Exception:
                pass
        except Exception:
            pass


@router.post("/api/vector/cleanup")
async def trigger_vector_cleanup():
    """手动触发向量库清理"""
    from agent.vector_cleanup import get_cleanup_service

    try:
        cleanup = get_cleanup_service()
        cleanup.run_full_cleanup()
        return {"status": "success", "message": "Vector database cleanup completed"}
    except Exception as e:
        logger.error(f"Vector cleanup failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/api/vector/stats")
async def get_vector_stats():
    """获取知识库统计信息（已迁移到 LightRAG）"""
    try:
        from niu_api.internal.lightrag_manager import get_lightrag

        rag = get_lightrag()
        if rag is None:
            return {"error": "LightRAG not initialized"}

        # LightRAG 知识图谱统计（直接读取 NetworkX 图的 O(1) 计数属性）
        graph_obj = rag.chunk_entity_relation_graph
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        node_count = nx_graph.number_of_nodes() if nx_graph else 0
        edge_count = nx_graph.number_of_edges() if nx_graph else 0

        return {
            "status": "lightrag",
            "node_count": node_count,
            "edge_count": edge_count,
            "message": "向量库已迁移到 LightRAG，旧 vectors.db 统计不再可用",
        }
    except Exception as e:
        return {"error": str(e)}

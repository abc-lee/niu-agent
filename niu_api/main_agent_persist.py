"""主 Agent 回合持久化（P3 D-1/D-2）——ChatQueue 与三个前端端点共用的唯一实现。

`persist_run_outcome`：RunOutcome → DB / SSE / holder。分支次序固定为
neutral → error → LLM_ERROR → cancelled 三形状 → 正常，任一端点都不得重排
（R6：重复实现漂移是 P3 的主要风险源）。

`make_on_complete`：runtime 线程（持 `_chat_lock`、handle 收口前）调用的同步回调，
把上面的协程桥回主事件循环；`CONTEXT_OVERFLOW` 再经 `call_soon_threadsafe` 交回
主循环触发压实。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from agent.main_runtime import RunOutcome
from loguru import logger

# 中性占位符：落库文本（错误细节不进 DB，避免污染下轮向量检索）
NEUTRAL_REPLY = "[系统繁忙，请重试]"


def _classify_degraded_reason(chat_error) -> str:
    """E4-12：降级回复错误类别——"timeout" | "internal"（DB 可追溯标记）。

    仅运维可追溯用——用户侧保持中性占位符（E2 定案，不泄露内部错误细节）。
    分类规则：
    - 锁超时路径（chat_error 为字符串 "timeout"）→ "timeout"
    - 异常对象类型名含 timeout（litellm.Timeout / APITimeoutError / TimeoutError 等瞬态）→ "timeout"
    - 其余（含 LLM 非超时类如 RateLimitError）→ "internal"
    """
    if chat_error == "timeout":
        return "timeout"
    if isinstance(chat_error, BaseException):
        if "timeout" in type(chat_error).__name__.lower():
            return "timeout"
    return "internal"


async def persist_run_outcome(store, outcome: RunOutcome, holder: dict) -> None:
    """RunOutcome → DB / SSE / holder["reply" | "message_id" | "error"]。

    只在主循环线程调用（`make_on_complete` 经 run_coroutine_threadsafe 桥回，或
    ChatQueue 的 submit 失败路径直接 await）。不在这里 drain supplement——由 runtime
    在释放锁之后处理（§4.1 步骤 6）。

    holder 是命令结果的唯一出口：每条分支都写 reply；落库成功的分支写 message_id，
    LLM_ERROR / 零输出取消（有意不落库）保持 None。
    """
    chat_error = outcome.error
    rv = outcome.return_value
    full_reply = outcome.result
    history_len = outcome.history_len
    holder.setdefault("message_id", None)
    from agent.generic.litellm_adapter import (
        extract_error_type,
        format_llm_error_for_user,
        is_litellm_error_type,
    )

    from niu_api.chat import notify_llm_error_sync, persist_agent_reply

    # neutral_degrade 先于 error：drain / submit 失败 / 命令从未执行 → 固定中性文案 +
    # degraded_reason=""，不调用 _classify_degraded_reason（没有真实错误可分类）
    if outcome.neutral_degrade:
        degraded_reply = NEUTRAL_REPLY
        holder["error"] = str(chat_error) if chat_error is not None else None
        logger.error(
            f"[MainPersist] Command never executed, writing neutral reply. "
            f"original_error={chat_error}"
        )
        try:
            message_id, _ = await persist_agent_reply(
                store, None, history_len, degraded_reply,
                source="electron", persisted_msgs=None, degraded_reason="",
            )
            holder["message_id"] = message_id
        except Exception as persist_e:
            logger.error(f"[MainPersist] Neutral reply persist failed: {persist_e}")
        holder["reply"] = degraded_reply
        return

    if chat_error is not None:
        # 异常路径：中性占位符落库 + 友好文案投递 + notify 解耦（E2）
        # DB 存中性占位符（错误细节不进 DB，避免污染下轮向量检索）；投递文本为友好文案
        # persisted_msgs 强制 None——runtime 异常路径已置 None（陈旧值语义陷阱）
        # source 强制 "electron"——与正常路径一致，避免被 notify_new_message 白名单过滤
        degraded_reply = NEUTRAL_REPLY  # 中性占位符落库（既有行为保持）
        holder["error"] = str(chat_error)
        type_name = type(chat_error).__name__ if isinstance(chat_error, BaseException) else None
        is_llm = bool(type_name) and is_litellm_error_type(type_name)
        if is_llm:
            push_text = format_llm_error_for_user(str(chat_error), type_name)  # 友好文案（独立于 persist 成败）
        # E4-12：降级是监控可见事件——非 LLM 异常（内部 bug/锁超时）error 级；
        # LLM 异常保持 warning（notify_llm_error_sync 已负责用户可见）。degraded_reason 落库。
        degraded_reason = _classify_degraded_reason(chat_error)
        _degraded_log = logger.error if not is_llm else logger.warning
        _degraded_log(
            f"[MainPersist] Chat error, writing degraded reply. original_error={chat_error}, degraded_reason={degraded_reason}"
        )
        try:
            message_id, _ = await persist_agent_reply(
                store, None, history_len, degraded_reply,
                source="electron", persisted_msgs=None,
                degraded_reason=degraded_reason,
            )
            holder["message_id"] = message_id
        except Exception as persist_e:
            logger.error(f"[MainPersist] Degraded reply persist failed: {persist_e}")
        # 统一赋值：persist 失败 DB 降级但投递仍友好（try/except 后单行）
        holder["reply"] = push_text if is_llm else degraded_reply
        if is_llm:
            notify_llm_error_sync(type_name, push_text, "chat_queue")  # notify 与 persist 解耦——persist 成败均推
        return

    # E2：rv 为 LLM_ERROR（无论是否取消）→ 错误文本不落库（刷新 Chat 从 DB 加载历史时自然
    # 消失），只 notify 用户可见；full_reply 已是源头友好文案，不重复 format（通道 2 双包风险）
    if isinstance(rv, dict) and rv.get("result") == "LLM_ERROR":
        error_msg = rv.get("error_msg", "") or ""
        error_type = rv.get("error_type")
        notify_llm_error_sync(
            error_type or extract_error_type(error_msg),
            format_llm_error_for_user(error_msg, error_type),
            "chat_queue",
        )
        holder["reply"] = full_reply
        return

    if outcome.cancelled:
        if rv is not None:
            # 协作式 STOPPED：保留本轮 rv 与 persisted/extracted——沿用已落库前缀的去重/@ 语义
            message_id, full_reply = await persist_agent_reply(
                store, rv, history_len, full_reply, source="electron",
                persisted_msgs=outcome.persisted_msgs,
                extracted_at_msgs=outcome.extracted_at_msgs,
            )
        elif full_reply.strip():
            # 有部分文本但无 rv：正常 persist（advanced 已决定这两字段是否 None；
            # degraded_reason 留空，不污染降级指标）
            message_id, full_reply = await persist_agent_reply(
                store, None, history_len, full_reply, source="electron",
                persisted_msgs=outcome.persisted_msgs,
                extracted_at_msgs=outcome.extracted_at_msgs,
                degraded_reason="",
            )
        else:
            # 零输出取消：不落库（有意偏差，计划 v1.9——与现状硬取消零输出不写 assistant 行
            # 一致），只写 holder「已停止」；message_id 保持 None
            holder["reply"] = "已停止"
            return
        holder["message_id"] = message_id
        holder["reply"] = full_reply
        return

    # 正常回合：persist（persisted_msgs/extracted_at_msgs 来自本轮快照，去重判据唯一来源；
    # source 强制 "electron"——所有 source 的 assistant 回复都走 electron SSE 推给前端）
    message_id, full_reply = await persist_agent_reply(
        store, rv, history_len, full_reply, source="electron",
        persisted_msgs=outcome.persisted_msgs,
        extracted_at_msgs=outcome.extracted_at_msgs,
    )
    holder["message_id"] = message_id
    holder["reply"] = full_reply


def make_on_complete(
    store, holder: dict, on_overflow: Callable[[], None] | None = None,
) -> Callable[[RunOutcome], None]:
    """D-2：给 RunCommand.on_complete 的同步回调（runtime 线程、持 `_chat_lock`）。

    现读 `niu_api.chat._main_loop`（D-2：绝不缓存），桥回主循环落库；不设短超时
    （I-5：持久化完成前 handle 不 done，下一条命令不会先跑），等待期间每 60s 记
    warning。主循环不可用 / 持久化异常只记录，并保证 holder 有兜底文案。

    `on_overflow` 必须是**零参闭包**（端点自己捕获 store/source，内部再惰性取
    `fire_and_forget_compaction`）；rv 为 CONTEXT_OVERFLOW 时在主循环线程调度它，
    与既有端点语义一致——persist 是否成功都要压实。
    """

    def on_complete(outcome: RunOutcome) -> None:
        from niu_api.chat import _main_loop

        loop = _main_loop
        if loop is None or loop.is_closed():
            # 桥不回去也必须给 user 行一个回复文案：holder 是命令结果唯一出口
            logger.error("[MainPersist] Main loop unavailable, outcome not persisted")
            holder.setdefault("message_id", None)
            holder["reply"] = NEUTRAL_REPLY
            return

        fut = asyncio.run_coroutine_threadsafe(
            persist_run_outcome(store, outcome, holder), loop,
        )
        waited = 0.0
        while True:
            try:
                fut.result(timeout=60.0)
                break
            except TimeoutError:
                if fut.done():
                    # 持久化协程自身抛 TimeoutError（不是这里的等待超时）
                    logger.error("[MainPersist] Persist coroutine raised TimeoutError")
                    break
                waited += 60.0
                logger.warning(
                    f"[MainPersist] on_complete persist still running ({waited:.0f}s)"
                )
            except Exception as exc:
                logger.error(f"[MainPersist] on_complete persist failed: {exc}")
                break

        if not holder.get("reply"):
            holder["reply"] = NEUTRAL_REPLY

        rv = outcome.return_value
        if on_overflow is not None and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {}) or {}
            logger.warning(
                f"[MainPersist] Main agent CONTEXT_OVERFLOW at "
                f"{overflow_data.get('tokens_used', 0)} tokens, scheduling mechanical compaction"
            )
            try:
                loop.call_soon_threadsafe(on_overflow)
            except Exception as exc:
                logger.warning(f"[MainPersist] overflow compaction scheduling dropped: {exc}")

    return on_complete

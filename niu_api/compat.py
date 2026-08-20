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
from datetime import datetime
from typing import NamedTuple

from agent.session import get_message_store
from agent.subagent import (
    _read_compress_target_tokens,
    _read_context_window_tokens,
    _read_max_output_tokens,
    _read_protect_recent_count,
    _read_warning_threshold,
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


class CascadeDeleteResult(NamedTuple):
    delete_ids: list[str]
    dangling_cleanups: list[dict]


class CascadeUpdateResult(NamedTuple):
    updates: list[dict]
    cascade_delete_ids: list[str]


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


def _cascade_tool_chain_deletes(fresh_messages, delete_ids: list[str], protected_ids: set[str] | None = None) -> CascadeDeleteResult:
    """级联删除：确保 tool 调用链完整性。

    当删除 assistant(tool_calls) 时，对应的 tool 输出也必须删除；
    当删除 tool 输出时，发起调用的 assistant(tool_calls) 也必须删除。

    protected_ids: 受保护的消息 ID 集合，级联命中这些 ID 时跳过删除并记录 warning。
    返回 (级联后的完整删除 ID 列表, 需要清理悬空 tool_calls 的更新列表)。
    当受保护的 assistant(tool_calls) 的 tool output 被删除时，assistant 本身不能删，
    但其 tool_calls 需要过滤掉已删除的 tool_call_id，避免 DB 中留下悬空引用。
    """
    delete_set = set(delete_ids)
    added = set()
    skipped_protected = set()
    # 收集受保护 assistant 的悬空 tool_calls 清理需求
    dangling_tc_cleanups: list[dict] = []  # [{"message_id": str, "dangling_tc_ids": set[str]}]

    # 预构建索引：消息 ID → 消息, tool_call_id → [tool 消息 ID]
    msg_map = {}
    tc_id_to_tool_mids: dict[str, list[str]] = {}
    for m in fresh_messages:
        mid = getattr(m, "id", "") or ""
        if mid:
            msg_map[mid] = m
        tc_call_id = getattr(m, "tool_call_id", "") or ""
        if getattr(m, "role", "") == "tool" and tc_call_id:
            tc_id_to_tool_mids.setdefault(tc_call_id, []).append(mid)

    def _try_add(mid: str):
        if mid in delete_set or mid in added:
            return
        if protected_ids and mid in protected_ids:
            skipped_protected.add(mid)
        else:
            added.add(mid)

    # Pass 1: 删除 assistant(tool_calls) → 级联删除对应的 tool 输出
    for mid in delete_ids:
        m = msg_map.get(mid)
        if not m:
            continue
        if getattr(m, "role", "") != "assistant":
            continue
        tcs = getattr(m, "tool_calls", None)
        if not tcs:
            continue
        try:
            if isinstance(tcs, str):
                tcs = json.loads(tcs)
            for tc in tcs:
                tc_id = tc.get("id", "")
                if tc_id:
                    for tool_mid in tc_id_to_tool_mids.get(tc_id, []):
                        _try_add(tool_mid)
        except (json.JSONDecodeError, TypeError):
            pass

    # Pass 2: 删除 tool 输出 → 级联删除发起调用的 assistant(tool_calls)
    deleted_tool_call_ids = set()
    for mid in delete_ids:
        m = msg_map.get(mid)
        if not m:
            continue
        if getattr(m, "role", "") == "tool":
            tc_call_id = getattr(m, "tool_call_id", "")
            if tc_call_id:
                deleted_tool_call_ids.add(tc_call_id)

    if deleted_tool_call_ids:
        for m in fresh_messages:
            mid = getattr(m, "id", "") or ""
            if getattr(m, "role", "") != "assistant" or mid in delete_set or mid in added:
                continue
            tcs = getattr(m, "tool_calls", None)
            if not tcs:
                continue
            try:
                if isinstance(tcs, str):
                    tcs = json.loads(tcs)
                for tc in tcs:
                    if tc.get("id", "") in deleted_tool_call_ids:
                        _try_add(mid)
                        # 只有当 assistant 未被保护跳过时，才级联删除其其他 tool 输出
                        # 受保护的 assistant 需要保持 tool 调用链完整性
                        if mid not in skipped_protected:
                            for tc2 in tcs:
                                tc2_id = tc2.get("id", "")
                                if tc2_id and tc2_id not in deleted_tool_call_ids:
                                    for tool_mid in tc_id_to_tool_mids.get(tc2_id, []):
                                        _try_add(tool_mid)
                        break
            except (json.JSONDecodeError, TypeError):
                pass

    # 收集所有被删除/级联删除的 tool 消息的 tool_call_id（Pass 1 + Pass 2 全部）
    for mid in added:
        m = msg_map.get(mid)
        if m and getattr(m, "role", "") == "tool":
            tc_call_id = getattr(m, "tool_call_id", "")
            if tc_call_id:
                deleted_tool_call_ids.add(tc_call_id)

    # Pass 3: 受保护的 assistant(tool_calls) 的 tool output 被删除 → 清理悬空 tool_calls
    if skipped_protected and deleted_tool_call_ids:
        for mid in skipped_protected:
            m = msg_map.get(mid)
            if not m or getattr(m, "role", "") != "assistant":
                continue
            tcs = getattr(m, "tool_calls", None)
            if not tcs:
                continue
            try:
                if isinstance(tcs, str):
                    tcs = json.loads(tcs)
                dangling = {tc.get("id", "") for tc in tcs if tc.get("id", "") in deleted_tool_call_ids}
                if dangling:
                    dangling_tc_cleanups.append({"message_id": mid, "dangling_tc_ids": dangling})
                    logger.info(f"[Tidy] Cascade: protected assistant {mid} has {len(dangling)} dangling tool_calls to clean")
            except (json.JSONDecodeError, TypeError):
                pass

    if skipped_protected:
        logger.warning(f"[Tidy] Cascade: skipped {len(skipped_protected)} protected messages from cascade deletes: {skipped_protected}")
    if added:
        logger.info(f"[Tidy] Cascade: adding {len(added)} tool-chain messages to deletes: {added}")
    return CascadeDeleteResult(delete_ids + list(added), dangling_tc_cleanups)


def _cascade_tool_chain_updates(fresh_messages, updates: list[dict]) -> CascadeUpdateResult:
    """级联更新：更新 assistant(tool_calls) 时清除悬空的 tool_calls，并返回需级联删除的 tool output ID。

    当 assistant 消息有 tool_calls 但其对应的 tool 输出已被删除时，
    将 tool_calls 清空，避免 LLM API 收到没有响应的工具调用。
    同时，如果 update 的目标消息有 tool_calls，清空 tool_calls，
    并将对应的 tool output 消息 ID 加入级联删除列表。

    Returns:
        tuple[list[dict], list[str]]: (更新后的 updates 列表, 需级联删除的 tool output 消息 ID 列表)
    """
    result = []
    cascade_delete_ids: list[str] = []
    for upd in updates:
        mid = upd.get("message_id", "")
        content = upd.get("content", "")
        if not mid or not content:
            result.append(upd)
            continue
        # 检查目标消息是否有 tool_calls
        msg = None
        for m in fresh_messages:
            if getattr(m, "id", "") == mid:
                msg = m
                break
        if msg and getattr(msg, "role", "") == "assistant":
            tcs = getattr(msg, "tool_calls", None)
            if tcs:
                try:
                    if isinstance(tcs, str):
                        tcs = json.loads(tcs)
                    if tcs:  # 非空 tool_calls → 清空 + 收集对应 tool output
                        logger.info(f"[Tidy] Cascade: clearing tool_calls on updated message {mid}")
                        # 收集所有 tool_call id，查找对应的 tool output 消息
                        tc_ids = {tc.get("id", "") for tc in tcs if tc.get("id", "")}
                        for m in fresh_messages:
                            m_id = getattr(m, "id", "") or ""
                            if getattr(m, "role", "") == "tool" and getattr(m, "tool_call_id", "") in tc_ids:
                                cascade_delete_ids.append(m_id)
                        if cascade_delete_ids:
                            logger.info(f"[Tidy] Cascade: marking {len(cascade_delete_ids)} orphan tool outputs for delete: {cascade_delete_ids}")
                        result.append({"message_id": mid, "content": content, "clear_tool_calls": True})
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
        result.append(upd)
    return CascadeUpdateResult(result, cascade_delete_ids)


async def _clean_dangling_tool_calls(store, message_id: str, valid_tcs: list[dict]):
    """清理受保护 assistant 消息的悬空 tool_calls，保留仍有 tool 响应的部分。

    直接用 aiosqlite 更新 tool_calls 字段，因为 store.update_message 不支持部分更新 tool_calls。
    """
    import aiosqlite
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE messages SET tool_calls = ? WHERE id = ?",
                       (json.dumps(valid_tcs, ensure_ascii=False), message_id))
        await db.commit()


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
    """清理 DB 中的压缩残留消息：孤立 tool 消息 + 空壳 assistant 消息。

    压缩可能遗留：
    1. 孤立 tool 消息（旧 bug 或模式2/3 没有完整性验证步骤）——tool_call_id 无对应 assistant tool_calls
    2. 空壳 assistant 消息——原始形态（content 空 + tool_calls 非空）的 assistant 的
       tool 输出被级联删除后，悬空清理清空其 tool_calls → 留下 content 空 + tool_calls 空 的空壳

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


def _parse_idx_list(s: str) -> set[int]:
    """解析 '1,3,5-10,12' 格式的 idx 列表，返回 set[int]"""
    result = set()
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a_val, b_val = int(a), int(b)
                if a_val > 0 and b_val > 0 and a_val <= b_val:
                    result.update(range(a_val, b_val + 1))
            except ValueError:
                pass
        else:
            try:
                val = int(part)
                if val > 0:
                    result.add(val)
            except ValueError:
                pass
    return result


def _find_protected_range(messages, min_protect_count: int) -> int:
    """Find the protection start index using user-turn-aware logic.

    1. From tail, count min_protect_count user/assistant messages -> idx_N
    2. From idx_N, scan upward (toward index 0) for the nearest role=user message:
       - First, skip any non-user messages (assistant/tool) going upward from idx_N
       - When a user message is found, keep scanning upward for consecutive user messages
       - Protect to the earliest user in the consecutive group
    3. Return idx_user (or idx_N if no user found above, or len(messages) if min_protect_count=0)

    All messages[index >= return_value] should be protected, including tool messages.
    """
    if min_protect_count <= 0 or not messages:
        return len(messages)  # no protection

    total = len(messages)

    # Step 1: from tail, count N user/assistant messages
    idx_N = total  # exclusive upper bound (one past last counted)
    count = 0
    for i in range(total - 1, -1, -1):
        role = getattr(messages[i], "role", "") if hasattr(messages[i], "role") else messages[i].get("role", "")
        if role in ("user", "assistant"):
            count += 1
            if count >= min_protect_count:
                idx_N = i
                break
    else:
        # Fewer than N user/assistant messages -> protect everything
        return 0

    # Step 2: from idx_N, scan upward (toward index 0) for nearest role=user message
    # Phase A: skip non-user messages (assistant, tool) going upward
    # Phase B: once user found, keep going up for consecutive user messages
    idx_user = idx_N
    found_user = False
    for i in range(idx_N, -1, -1):
        role = getattr(messages[i], "role", "") if hasattr(messages[i], "role") else messages[i].get("role", "")
        if role == "user":
            idx_user = i
            found_user = True
            # keep scanning upward for consecutive user messages
        elif found_user:
            # we found user(s) but hit a non-user -> stop
            break
        # if not found_user and role != "user", continue scanning (skip assistant/tool)

    return idx_user

def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None, end_cursor_id: str | None = None, protect_recent: int = 0, exclude_protected: bool = False) -> str:
    """
    构建增量消息文本：只包含游标之后的新消息。

    Args:
        messages: 全量消息列表
        last_cursor_id: 上次处理到的消息 UUID（空字符串表示全量）
        out_msg_ids: 输出参数，收集增量消息的 UUID 列表
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不注解
        end_cursor_id: 上界游标 UUID，只生成到该消息为止（含该消息），None 则到末尾
        protect_recent: 对最后 N 条消息加 [PROTECTED] 标签（0 表示不加）

    Returns:
        格式化的消息文本
    """
    # 找到下界游标位置
    cursor_idx = -1
    if last_cursor_id:
        for i, msg in enumerate(messages):
            msg_id = getattr(msg, "id", "") or ""
            if msg_id == last_cursor_id:
                cursor_idx = i
                break
        if cursor_idx < 0:
            logger.warning(f"[Tidy] Cursor UUID {last_cursor_id} not found in message list, degrading to full processing")

    # 找到上界游标位置
    end_idx = len(messages) - 1
    if end_cursor_id:
        found = False
        for i, msg in enumerate(messages):
            if (getattr(msg, "id", "") or "") == end_cursor_id:
                end_idx = i
                found = True
                break
        if not found:
            logger.warning(f"[Tidy] End cursor UUID {end_cursor_id} not found in message list, degrading to full range")
            end_idx = len(messages) - 1

    # 计算有效范围：[start, effective_end)
    start = cursor_idx + 1 if cursor_idx >= 0 else 0
    effective_end = end_idx + 1  # 包含 end_cursor 本身

    if start >= effective_end:
        return "（无新增消息）"

    # 构建带原始位置的消息列表（保留原始 idx）
    range_messages_with_pos = [(i, msg) for i, msg in enumerate(messages[start:effective_end])]

    lines = []
    total_count = len(range_messages_with_pos)
    # 预计算保护位置：从尾部向前找 N 条 user/assistant 消息的相对位置
    _protected_positions = None
    if protect_recent > 0:
        # User-turn-aware protection: protect from nearest user message at/below the N-th user/assistant from tail
        _range_msgs = [m for _, m in range_messages_with_pos]
        _protect_start = _find_protected_range(_range_msgs, protect_recent)
        _protected_positions = set(range(_protect_start, total_count))
    display_idx = 0
    for rel_pos, (orig_pos, msg) in enumerate(range_messages_with_pos):
        msg_id = getattr(msg, "id", "") or ""
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + orig_pos) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + orig_pos]}tokens "
        # protect_recent: 对最后 N 条 user/assistant 消息加 [PROTECTED] 标签（不保护 role=tool 的工具输出）
        protected_label = ""
        if protect_recent > 0 and _protected_positions is not None and rel_pos in _protected_positions:
            if exclude_protected:
                continue  # 排除 PROTECTED 消息：不加入 out_msg_ids 和 lines
            protected_label = "[PROTECTED] "
        display_idx += 1
        out_msg_ids.append(msg_id)
        lines.append(f"[id:{msg_id}] [idx:{display_idx}] {token_annotation}{msg.role}: {protected_label}{content}")

    if not lines:
        return "（无新增消息）"

    return f"共 {len(lines)} 条新消息\n\n" + "\n".join(lines)


def _build_compress_history(
    messages,
    msg_tokens: list | None = None,
    out_msg_ids: list | None = None,
    protect_recent: int = 0,
    exclude_protected: bool = False,
) -> tuple[list[dict], dict[int, str]]:
    """构造 context-manager 模式二的 history 列表（每条 message 加 idx 前缀）。

    与 _build_incremental_msg_text 的区别：
    - 输出 history 列表（role/content/tool_calls/tool_call_id 原样），而非序列化文本
    - content 开头加 `[idx:N] Ntokens ` 前缀（简易 idx，不用 UUID）
    - 单条 message 不会超限（每条就是原大小 + 前缀）
    - 同步排除孤立 tool 消息：若父 assistant 被 PROTECTED 排除，其 tool 消息也排除
      （避免 agent_runner_loop 过滤孤立 tool 导致 LLM 看到的 idx 不连续）

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content/tool_calls/tool_call_id）
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不加 tokens 前缀
        out_msg_ids: 输出参数，收集保留消息的真实 ID 列表（与 idx 顺序一致）
        protect_recent: 对最后 N 条 user/assistant 消息加 PROTECTED 标签（0 表示不加）
        exclude_protected: True 则排除 PROTECTED 消息（不进 history、不进 out_msg_ids、不分配 idx）

    Returns:
        (history, idx_to_id):
        - history: [{"role":..., "content": "[idx:N] Ntokens ...原content", "tool_calls"?:..., "tool_call_id"?:...}, ...]
        - idx_to_id: {idx: 真实 message_id}，用于解析 context-manager 输出的 keep=/update=
    """
    if out_msg_ids is None:
        out_msg_ids = []

    total_count = len(messages)
    _protected_positions: set[int] = set()
    if protect_recent > 0:
        _protect_start = _find_protected_range(messages, protect_recent)
        _protected_positions = set(range(_protect_start, total_count))

    # 第一遍：确定哪些位置被排除（PROTECTED 排除 + 孤立 tool 同步排除）
    excluded_positions: set[int] = set()

    # 1) PROTECTED 排除
    if exclude_protected:
        for rp in _protected_positions:
            excluded_positions.add(rp)

    # 2) 孤立 tool 同步排除：若 tool 消息的父 assistant（持有对应 tool_call_id）被排除，则 tool 也排除
    #    收集所有被排除的 assistant 的 tool_call_id
    orphaned_tool_call_ids: set[str] = set()
    for rp in excluded_positions:
        m = messages[rp]
        if getattr(m, "role", "") == "assistant" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                if tc_id:
                    orphaned_tool_call_ids.add(tc_id)
    # 排除孤立 tool 消息
    for rp, m in enumerate(messages):
        if getattr(m, "role", "") == "tool":
            tc_id = getattr(m, "tool_call_id", "") or ""
            if tc_id in orphaned_tool_call_ids:
                excluded_positions.add(rp)

    # 第二遍：构造 history（只含未被排除的消息，idx 连续编号）
    history: list[dict] = []
    idx_to_id: dict[int, str] = {}
    display_idx = 0

    for rel_pos, msg in enumerate(messages):
        if rel_pos in excluded_positions:
            continue

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


def _build_plain_history(messages, out_msg_ids: list | None = None) -> tuple[list[dict], dict[int, str]]:
    """构造带 [N] 极简前缀的 history 列表 + 简易ID↔UUID 映射（仿 context-manager 的 _build_compress_history）。

    用于非压缩子 Agent（entity-extractor / dream-evolver / journal-agent）的 force/sleep 调用：
    - history 每条 content 前缀 "[N] "（N 是 1-based 简易编号）
    - 同步构建 idx_to_id 映射 {N: 真实UUID}，供程序解析子 Agent 输出的 processed_up_to=N 后查 UUID 更新游标
    - 不排除 PROTECTED 消息（所有消息都该看到）
    - 不排除孤立 tool（保持原顺序，子 Agent 自己判断）

    与 _build_compress_history 的区别：
    - 前缀极简 "[N] "（不是 "[idx:N] Ntokens "）
    - 不排除 PROTECTED / 不排除孤立 tool（调用方按需在调用前过滤 PROTECTED，如 entity force 全量路径，详见 Architecture §6）
    - 不含 token 标注（非压缩子 Agent 不需要做压缩决策）

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content/tool_calls/tool_call_id）
        out_msg_ids: 输出参数，收集消息的真实 ID 列表（与 history 等长同顺序，用于游标推进兜底）

    Returns:
        (history, idx_to_id):
        - history: [{"role":..., "content": "[N] 原content", "tool_calls"?:..., "tool_call_id"?:...}, ...]
        - idx_to_id: {N: 真实 message_id}，用于解析子 Agent 输出的 processed_up_to=N
    """
    if out_msg_ids is None:
        out_msg_ids = []

    history: list[dict] = []
    idx_to_id: dict[int, str] = {}
    display_idx = 0

    for msg in messages:
        msg_id = getattr(msg, "id", "") or ""
        role = getattr(msg, "role", "user")
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)

        display_idx += 1
        out_msg_ids.append(msg_id)
        idx_to_id[display_idx] = msg_id

        # 极简前缀 [N]（不带 UUID / tokens / role）
        prefix = f"[{display_idx}] "
        entry: dict = {"role": role, "content": prefix + content}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id

        history.append(entry)

    return history, idx_to_id


def _parse_processed_up_to(response: str) -> int | None:
    """从子 Agent 输出中提取 processed_up_to=N 的 N 值。

    支持格式（大小写不敏感）：
    - "processed_up_to=15"
    - "processed_up_to: 15"
    - "processed_up_to 15"
    - 匹配第一个有效整数

    Args:
        response: 子 Agent 的完整输出文本

    Returns:
        N (int) 或 None（未找到或格式无效）
    """
    import re
    if not response:
        return None
    # 大小写不敏感，支持 = / : / 空格 三种分隔
    # 字符类 [=:\s] 同时匹配 =、: 和纯空格分隔（如 "processed_up_to 15"）
    match = re.search(r'processed_up_to\s*[=:\s]\s*(\d+)', response, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _strip_analysis(response: str) -> str:
    """剥离 <analysis>...</analysis> 块，只保留 keep/update/cursor 部分。

    处理以下情况：
    1. 闭合的 <analysis>...</analysis>（含跨行）
    2. 未闭合的 <analysis>（有开始无结束，剥离到字符串末尾）
    3. 大小写不敏感（<ANALYSIS> 也识别）
    4. 无 analysis 块时原样返回
    """
    # 先匹配闭合的 <analysis>...</analysis>
    cleaned = re.sub(r'<analysis>.*?</analysis>\s*', '', response, flags=re.DOTALL | re.IGNORECASE)
    # 再处理未闭合的 <analysis>（LLM 写了开始标签但没写结束）
    cleaned = re.sub(r'<analysis>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def _build_mode2_prompt(display_tokens: int, compress_target_tokens: int, usage_percent: float, compress_history: list) -> str:
    """构造模式二 task prompt（瘦身版：去方法论重复，禁止报告，直接输出两行）。

    方法论（三份划分/会话单元/工具输出级联/摘要规范/转义规则）已在系统提示词
    （config/agents/context-manager.md）完整定义——task prompt 不再内联重复。
    输出契约提醒保留（靠近输出点、指令性强、成本极低）。
    CRITICAL 短语必须保留：context-manager.md L194-199 用 prompt 含
    "CRITICAL: 你只有一轮机会" 判定模式二"一轮方案"分支激活（门控），
    删除会导致模式二走工具调用路径。门控说明只写 docstring，不进 prompt 文本。
    """
    return f"""本次任务：对上方历史消息执行模式二压缩。压缩规则详见你的系统提示词（模式二章节），不再重复。

当前上下文状态：
- 参与压缩的消息数：{len(compress_history)}（受保护消息已排除）
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{compress_target_tokens}
- 需释放至少 {display_tokens - compress_target_tokens} tokens

CRITICAL: 你只有一轮机会完成压缩决策。

输出要求（严格，违反即失败）：
- 只输出两行：keep= 行和 update= 行
- 禁止输出任何其他内容：禁止 <analysis>、禁止分析过程、禁止解释、禁止总结报告、禁止 markdown 代码块
- 格式示例（仅格式参考，内容由你按规则决定）：
  keep=1,5,15,30,50,75,100,105,115,150,180,200
  update=1|[摘要] 指令：xxx | 流程：xxx | 结果：xxx;5|[摘要] 指令：xxx | 流程：xxx | 结果：xxx
- update= 多条用分号 `;` 分隔；摘要内容含分号用全角 `；`；段内避免 `|`
- update 的 idx 必须在 keep= 中；未列在 keep= 中的消息将被删除

REMINDER: 禁止调用任何工具，直接输出 keep=/update= 两行，仅此两行。"""


def _build_force_prompt(display_tokens: int, compress_target_tokens: int, usage_percent: float,
                        force_history: list, last_compress_id: str | None, dream_idx_in_force: int) -> str:
    """构造模式三 force task prompt（瘦身版：去方法论重复，禁止报告，直接输出三行）。

    方法论（保留优先级/强度量化表/dream 安全边界规则）已在系统提示词（模式三章节）完整定义。
    last_compress_id 与 dream_idx_in_force 是本次调用的专属参数，必须注入。
    """
    return f"""本次任务：对上方历史消息执行模式三强制压缩。压缩规则详见你的系统提示词（模式三章节），不再重复。

当前上下文状态：
- 参与压缩的消息数：{len(force_history)}（受保护消息已排除）
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{compress_target_tokens}
- 需释放至少 {display_tokens - compress_target_tokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

安全边界：idx > {dream_idx_in_force} 的消息（dream-evolver 未提取知识），
不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
注：受保护消息已从列表中排除，无需处理。

CRITICAL: 你只有一轮机会完成压缩决策。

输出要求（严格，违反即失败）：
- 只输出三行：keep= 行、update= 行、cursor= 行
- 禁止输出任何其他内容：禁止 <analysis>、禁止分析过程、禁止解释、禁止总结报告、禁止 markdown 代码块
- 格式示例（仅格式参考，内容由你按规则决定）：
  keep=1,5,15,30,50,75,100,105,115,150,180,200
  update=1|[摘要] 指令：xxx | 流程：xxx | 结果：xxx;5|[摘要] 指令：xxx | 流程：xxx | 结果：xxx
  cursor=200
- update= 多条用分号 `;` 分隔；摘要内容含分号用全角 `；`；段内避免 `|`
- update 的 idx 必须在 keep= 中；cursor= 是操作范围内 idx 最大且仍存在的消息 idx
- 未列在 keep= 中的消息将被删除

REMINDER: 禁止调用任何工具，直接输出 keep=/update=/cursor= 三行，仅此三行。"""


def _build_compress_llm_config() -> dict:
    """压缩专用 LLM 配置：按知识图谱（lightrag 段）用户配置 + max_tokens（输出预算）。

    用户拍板 2026-08-18：程序不预设过滤——thinking/effort 按用户 lightrag 段配置
    原样透传（testAndSave 已把关组合可用性——保存的配置组合一定可用，压缩送同样
    组合不 400）；不再注入 thinking disabled（lightrag 段用户配置已含）、不再置
    reasoning_effort。只注入 max_tokens（压缩结构化三行输出的输出预算）。
    无入参（内部 refetch，不留死参数）。
    """
    from niu_api.llm_proxy import get_llm_config

    cfg = dict(get_llm_config(use_lightrag_config=True))  # lightrag 段：thinking/effort 用户配置原样
    litellm_kwargs = {**cfg.get("litellm_kwargs", {}), "max_tokens": _read_max_output_tokens()}
    cfg["litellm_kwargs"] = litellm_kwargs
    return cfg


async def _emergency_clear(
    history: list,
    msg_ids: list,
    protect_recent_count: int,
    store,
    session_id: str,
    mode: str,
) -> dict:
    """截断时的应急清空：保留最近完整用户会话段落，上面全删，最旧那条改为"压缩失败"摘要。

    - history: 压缩历史消息列表（受保护消息已排除），按 idx 顺序排列（list[dict]，无 id 字段）
    - msg_ids: 与 history 等长、同顺序的真实 message_id 列表（来自 out_msg_ids）
    - protect_recent_count: 最少保护的 user/assistant 消息对数（来自配置）
    - store: MessageStore，用于 delete_messages_by_ids / update_message
    - session_id: 会话 ID（仅用于日志，delete_messages_by_ids 不需要）
    - mode: "sleep" 或 "force"（用于返回值）

    返回 {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}
    """
    total = len(history)
    # Use user-turn-aware protection to find the keep boundary
    _protect_start = _find_protected_range(history, protect_recent_count)
    if _protect_start <= 0 or _protect_start >= len(msg_ids):
        logger.warning(
            f"[Compact] history len {len(msg_ids)}, all protected, no clear needed"
        )
        return {
            "status": "skipped",
            "mode": mode,
            "reason": "truncated, no clear needed (all protected)",
        }

    delete_ids = list(msg_ids[:_protect_start])
    oldest_kept_id = msg_ids[_protect_start]

    # 最旧保留条改为"压缩失败"摘要
    await store.update_message(
        message_id=oldest_kept_id,
        content=(
            "[压缩失败，历史信息丢失] 上下文压缩时 LLM 输出截断，此条之上的历史已删除。"
            "可通过 journal.md 和知识图谱回溯。"
        ),
    )

    # 删除上面的消息
    await store.delete_messages_by_ids(delete_ids)

    logger.warning(
        f"[Compact] Emergency cleared: deleted {len(delete_ids)} msgs, kept recent {len(msg_ids) - _protect_start}, marked oldest ({oldest_kept_id}) as lost-summary"
    )
    return {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}

def _build_degraded_config() -> dict:
    """构造降级第一步的 LLM 配置（与 _build_compress_llm_config 同构，无入参）。

    用户拍板 2026-08-18：降级按知识图谱（lightrag 段）用户配置原样透传——
    thinking/effort 不注入不降级（程序不干预用户配置，组合可用性由 testAndSave
    把关）。effort_map 降级已删除（T2：disabled 路径下 high→medium 仍 400——
    有害代码而非死代码）。只注入 max_tokens（输出预算）。
    """
    from niu_api.llm_proxy import get_llm_config

    cfg = dict(get_llm_config(use_lightrag_config=True))  # lightrag 段：thinking/effort 用户配置原样
    litellm_kwargs = {**cfg.get("litellm_kwargs", {}), "max_tokens": _read_max_output_tokens()}
    cfg["litellm_kwargs"] = litellm_kwargs
    return cfg

def _halve_history(compress_history: list, compress_msg_ids: list) -> tuple[list, list, list, int]:
    """
    砍半消息历史。在 N/2 附近向前找第一条 role=user 消息对齐截断。
    返回 (后半段 history, 后半段 msg_ids, 前半段 msg_ids, cut_idx)。
    cut_idx 是 0-based Python 列表索引。
    [idx:N] 前缀需要重新编号（由 _renumber_history 执行）。
    """
    total = len(compress_history)
    if total == 0:
        return [], [], [], 0
    target_cut = total // 2

    # 从 target_cut 向前找第一条 role=user
    cut_idx = target_cut
    found_user = False
    while cut_idx >= 0:
        msg = compress_history[cut_idx]
        if isinstance(msg, dict) and msg.get("role") == "user":
            found_user = True
            break
        cut_idx -= 1

    # 没找到 user 消息 → 从 target_cut 截断
    if not found_user:
        cut_idx = target_cut

    halved_history = compress_history[cut_idx:]
    halved_msg_ids = compress_msg_ids[cut_idx:]
    removed_msg_ids = compress_msg_ids[:cut_idx]

    return halved_history, halved_msg_ids, removed_msg_ids, cut_idx


def _renumber_history(history: list) -> list:
    """重新编号 history 中的 [idx:N] 前缀为连续的 1, 2, 3...（只替换第一个前缀）"""
    import re

    renumbered = []
    for i, msg in enumerate(history):
        if isinstance(msg, dict) and "content" in msg:
            content = msg["content"]
            content = re.sub(r"\[idx:\d+\]", f"[idx:{i + 1}]", content, count=1)
            msg = {**msg, "content": content}
        renumbered.append(msg)
    return renumbered

def _compact_with_degradation_sync(
    agent_name: str,
    prompt: str,
    compress_history: list,
    compress_msg_ids: list,
    llm_config: dict,
    prompt_builder: callable,
    prompt_builder_kwargs: dict,
    call_fn: callable,
    stop_aware: bool = False,
) -> tuple[str | None, list, list | None]:
    """
    三级降级压缩（纯逻辑，不含 IO）。
    返回值：
    - 成功（未砍半）：(压缩方案字符串, 实际使用的 compress_msg_ids, None)
    - 成功（砍半后）：(压缩方案字符串, 后半段 compress_msg_ids, 前半段被砍掉的 msg_ids)
    - 失败：(None, 原始 compress_msg_ids, None)

    注意：本函数不执行原始调用——原始调用由调用方在调用本函数之前完成。
    本函数只执行降级第一步（call 1）和降级第二步（call 2）。
    """
    # 降级第一步：按知识图谱（lightrag 段）配置原样重试（T2——程序不干预 thinking/effort）
    # 门控只判 thinking type（2026-08-18 用户拍板：effort 条件移除——程序不预设过滤；
    # lightrag 段默认 disabled → 门控 False → step1 不执行，直接 step2 halve history。
    # 实际降级手段 = halve history；max_tokens 是基础输出预算非降级步骤）
    thinking_cfg = llm_config.get("litellm_kwargs", {}).get("thinking", {})
    thinking_enabled = isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled"

    if thinking_enabled:
        degraded_config = _build_degraded_config()

        step1_result = call_fn(
            agent_name=agent_name,
            task=prompt,
            llm_config=degraded_config,
            mcp_client=None,
            context_fifo_threshold=-1,
            history=compress_history,
            bypass_at_prefix=True,
        )

        # SUBAGENT_ERROR 检查（LLM 调用失败）
        if step1_result and step1_result.startswith("SUBAGENT_ERROR:"):
            logger.warning(f"[Compact] Degradation step 1 LLM error: {step1_result}")
            return None, compress_msg_ids, None

        # 降级第一步成功（非截断、非错误）
        if step1_result and not step1_result.startswith("COMPACT_TRUNCATED:"):
            logger.info("[Compact] Degradation step 1 (disable thinking) succeeded")
            return step1_result, compress_msg_ids, None

        if step1_result and step1_result.startswith("COMPACT_TRUNCATED:"):
            logger.warning("[Compact] Degradation step 1 still truncated, trying step 2 (halve history)")
        elif not step1_result:
            logger.warning("[Compact] Degradation step 1 returned empty, trying step 2 (halve history)")
    else:
        logger.info("[Compact] Thinking already disabled, skipping step 1, going directly to step 2 (halve history)")

    # 停止检查
    try:
        from agent.runner import is_stop_requested
        if stop_aware and is_stop_requested():
            logger.warning("[Compact] Stop requested during degradation, aborting")
            return None, compress_msg_ids, None
    except Exception:
        pass

    # 降级第二步：砍半消息
    halved_history, halved_msg_ids, removed_msg_ids, cut_idx = _halve_history(
        compress_history, compress_msg_ids
    )

    if len(halved_history) <= 1:
        logger.warning("[Compact] Degradation step 2: halved history too small, aborting")
        return None, compress_msg_ids, None

    # 重新构建 prompt（先 shallow copy 再修改，避免修改原始入参）
    kwargs = dict(prompt_builder_kwargs)

    # Force 路径 dream_idx 重新计算
    # dream_idx 是 1-based（来自 _f_id_to_idx 的 _i+1），cut_idx 是 0-based（Python 列表索引）
    # dream 边界被砍掉的条件：1-based D <= 0-based cut_idx（等价于 0-based D-1 <= cut_idx-1）
    orig_dream_idx = kwargs.get("dream_idx_in_force", 0)
    if orig_dream_idx and orig_dream_idx > 0:
        if orig_dream_idx <= cut_idx:
            # dream 边界在前半段（被砍掉），后半段全部受保护无法删除
            logger.warning(f"[Compact] dream_idx={orig_dream_idx} <= cut_idx={cut_idx}, cannot halve")
            return None, compress_msg_ids, None
        kwargs["dream_idx_in_force"] = orig_dream_idx - cut_idx

    # 重新编号
    halved_history = _renumber_history(halved_history)

    # Mode-2 用 compress_history，Force 用 force_history
    if "compress_history" in kwargs:
        kwargs["compress_history"] = halved_history
    if "force_history" in kwargs:
        kwargs["force_history"] = halved_history

    rebuilt_prompt = prompt_builder(**kwargs)

    step2_result = call_fn(
        agent_name=agent_name,
        task=rebuilt_prompt,
        llm_config=degraded_config if thinking_enabled else llm_config,
        mcp_client=None,
        context_fifo_threshold=-1,
        history=halved_history,
        bypass_at_prefix=True,
    )

    # SUBAGENT_ERROR 检查
    if step2_result and step2_result.startswith("SUBAGENT_ERROR:"):
        logger.warning(f"[Compact] Degradation step 2 LLM error: {step2_result}")
        return None, compress_msg_ids, None

    if step2_result and not step2_result.startswith("COMPACT_TRUNCATED:"):
        logger.info("[Compact] Degradation step 2 (halve history) succeeded")
        return step2_result, halved_msg_ids, removed_msg_ids

    logger.warning("[Compact] Degradation step 2 still truncated, aborting")
    return None, compress_msg_ids, None

def _estimate_text_tokens(text: str) -> int:
    """粗略估算文本 token 数（中文约1.5字/token，英文约4字/token，取中间值2字/token）"""
    return len(text) // 2


def _truncate_preserving_tail(text: str, max_tokens: int) -> str:
    """截断文本，保留末尾第三份（最近）消息（第一份从开头截断）。
    消息列表在 prompt 末尾，开头是第一份（idx小的），末尾是第三份（idx大的）。
    截断第一份保留第三份，确保 LLM 能看到需要保护的消息。"""
    max_chars = max_tokens * 2  # 反向估算字符数
    if len(text) <= max_chars:
        return text
    # 保留末尾第三份部分，截断开头第一份
    kept_tail = text[-max_chars:]
    # 找到第一个完整的消息行（以 [id: 开头）
    first_line_pos = kept_tail.find("[id:")
    if first_line_pos > 0:
        kept_tail = kept_tail[first_line_pos:]
    # 更新消息计数
    line_count = kept_tail.count("[id:")
    return f"共约 {line_count} 条消息（第一份部分已省略。当前可见消息均属于第二份和第三份，按相对位置划分区域即可）\n\n" + kept_tail


def _truncate_preserving_both(text: str, max_tokens: int) -> str:
    """双向截断：保留开头指令 + 末尾第三份（最近）消息，截断中间第一份消息。
    用于全量范围压缩模式，确保 LLM 能同时看到指令和受保护消息。
    结构化截断：以"消息列表："为分割点，确保指令部分完整保留。"""
    max_chars = max_tokens * 2
    if len(text) <= max_chars:
        return text
    # 找到"消息列表："分割点，确保指令部分完整
    msg_marker = "\n消息列表：\n"
    marker_pos = text.find(msg_marker)
    if marker_pos < 0:
        msg_marker = "消息列表："
        marker_pos = text.find(msg_marker)
    if marker_pos > 0 and marker_pos < max_chars * 0.5:
        # 指令部分在合理范围内，完整保留指令 + 截断消息列表
        head = text[:marker_pos + len(msg_marker)]
        msg_text = text[marker_pos + len(msg_marker):]
        # 消息列表部分：保留末尾第三份消息
        tail_budget = max_chars - len(head) - 200
        if tail_budget > 0 and len(msg_text) > tail_budget:
            tail = msg_text[-tail_budget:]
            first_msg = tail.find("[id:")
            if first_msg > 0:
                tail = tail[first_msg:]
            msg_count = tail.count("[id:")
            return head + f"[第一份消息已省略，保留第三份 {msg_count} 条消息。可见消息从第一份中后段开始，按相对位置划分区域]\n\n" + tail
        return text
    # fallback：纯字符截断（指令部分过大）
    # 先提取保护 ID 行，确保 fallback 路径不丢失关键信息
    protected_line = ""
    for _line in text.split('\n'):
        if _line.startswith('保护消息ID:'):
            protected_line = _line
            break
    head_chars = int(max_chars * 0.2)
    tail_chars = max_chars - head_chars - 200
    head = text[:head_chars]
    last_nl = head.rfind('\n')
    if last_nl > head_chars // 2:
        head = head[:last_nl]
    # 如果 head 中没有保护 ID 行，追加到 head 末尾
    if protected_line and '保护消息ID:' not in head:
        head = head + '\n' + protected_line
    tail = text[-tail_chars:]
    first_msg = tail.find("[id:")
    if first_msg > 0:
        tail = tail[first_msg:]
    msg_count = tail.count("[id:")
    return head + "\n\n[中间第一份消息已省略，保留第三份 " + str(msg_count) + " 条消息。可见消息从第一份中后段开始，按相对位置划分区域]\n\n" + tail


def _build_journal_task() -> str:
    """构建 journal-agent 的 task prompt（纯指令，消息以 history 形式逐条传入）。

    Returns:
        纯指令 task prompt 字符串（含 @end processed_up_to=N 说明，程序据此推进游标）
    """
    return """以上是对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，1-based）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""


def _write_cursor_with_lock(cursor_path, data: dict) -> None:
    """带文件锁保护的游标写入 — 防止 handler/compat/runner 并发竞争。"""
    lock_path = cursor_path.with_suffix(".lock")
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as lock_f:
        _flock(lock_f)
        try:
            cursor_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            _funlock(lock_f)



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

def _flock(lock_f) -> None:
    """跨平台文件锁。Unix 用 fcntl.flock，Windows 用 msvcrt.locking。"""
    import sys
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(lock_f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(lock_f, fcntl.LOCK_EX)

def _funlock(lock_f) -> None:
    """跨平台文件解锁。"""
    import sys
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock_f, fcntl.LOCK_UN)


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


def _truncate_task_for_subagent(task: str, max_tokens: int) -> str:
    """
    截断子Agent的 task 内容，确保不超过 max_tokens。
    保留 task 开头（包含指令和状态信息），截断末尾的消息列表。
    在截断位置添加截断标记。
    """
    if not task:
        return task
    try:
        from agent.token_calculator import TokenCalculator
        token_count = TokenCalculator.get().count_text(task)
    except Exception:
        # 回退估算：2 字符/token
        token_count = max(1, len(task) // 2)

    if token_count <= max_tokens:
        return task

    # 按 token 比例估算需要保留的字符数
    keep_ratio = max_tokens / token_count
    keep_chars = int(len(task) * keep_ratio * 0.9)  # 留 10% 安全余量

    truncated = task[:keep_chars]
    # 找到最近的完整行
    last_newline = truncated.rfind('\n')
    if last_newline > keep_chars // 2:
        truncated = truncated[:last_newline]

    truncated += "\n\n[内容已截断：原始 task 超过 token 限制，仅保留前半部分消息。请基于已有内容完成整理。]"
    logger.warning(f"[Tidy] Task truncated: {token_count} -> ~{max_tokens} tokens, {len(task)} -> {len(truncated)} chars")
    return truncated


_MAX_TOOL_RESULT_CHARS = 30000

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
    try:
        context_window = _read_context_window_tokens()
        real_tokens = 0
        if agent:
            # 子 Agent：从 SubagentRegistry 读运行中 handler 的真实 prompt_tokens
            try:
                from agent.subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(agent)
                if instance is not None:
                    handler_ref = getattr(instance, "handler", None) or getattr(instance, "suspended_handler", None)
                    real_tokens = getattr(handler_ref, "_last_prompt_tokens", 0) or 0
            except Exception:
                pass
        else:
            try:
                from niu_api.chat import get_or_create_runner
                runner = get_or_create_runner()
                real_tokens = getattr(getattr(runner, 'handler', None), '_last_prompt_tokens', 0) or 0
            except Exception:
                pass
        if real_tokens > 0:
            context_usage = real_tokens / context_window if context_window > 0 else 0.0
        elif not agent:
            # 主 Agent 无真实 tokens 时 fallback 估算全库消息；子 Agent 无此概念，直接 0
            context_usage = (await compute_context_usage_estimate(store=store, context_window=context_window)) or 0.0
    except Exception:
        context_usage = 0.0

    return StatsResponse(messages=messages, uptime=uptime, files=files, persons=persons, notes=notes, context_usage=context_usage)


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

        # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞）
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat Session] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"triggering force compression (blocking)"
            )
            try:
                await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
            except TimeoutError:
                logger.warning("[Chat Session] Force compression skipped: tidy lock held by another operation")
                tidy_result = {"status": "skipped", "reason": "lock_busy"}
            else:
                try:
                    tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"}, chat_lock_already_held=True)
                finally:
                    _tidy_lock.release()
                logger.info(f"[Chat Session] Force compression result: {tidy_result.get('status')}")
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
        full: If True, return full content (for context-manager)
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

# 游标文件列表（清空消息后必须一并复位，否则游标指向已删除消息）
_ALL_CURSOR_FILES = [
    "last_entity_extract.json",
    "last_dream_evolve.json",
    "last_compress.json",
    "last_journal.json",
]


async def _reset_all_cursors() -> None:
    """删除全部增量处理游标文件（消息清空后调用，避免游标指向已删消息）。"""
    from pathlib import Path
    for cursor_name in _ALL_CURSOR_FILES:
        cursor_p = Path.home() / ".niu" / cursor_name
        try:
            if cursor_p.exists():
                cursor_p.unlink()
        except OSError as e:
            logger.warning(f"Failed to reset cursor file {cursor_name}: {e}")


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

    body 可选键：
        force_tidy (bool): True 时清空前先跑 force 整理（entity→dream→journal，skip_compress）
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    force_tidy = bool(body.get("force_tidy"))  # snake_case —— main.js 发送一致

    # 先请求停止当前 Agent 工作（全局 stop 标志）
    from agent.runner import clear_stop, request_stop
    request_stop()

    # 获取锁，防止与正在进行的 chat 冲突
    # 超时增加到 120 秒：/clear 的 busy 分支先发 /stop 停主 Agent，持锁者在 finally 释放；
    # 120s 覆盖 persist + CONTEXT_OVERFLOW force-compress；若主 Agent 卡在子 agent tool 超时
    # → 优雅失败需重试。
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=120.0)
    except TimeoutError:
        logger.warning("[clear_chat] _chat_lock 120s timeout, clear rejected")
        clear_stop()  # 防止停止标志残留，影响后续定时任务
        return {"success": False, "error": "系统正忙，请稍后再试"}

    try:
        clear_stop()  # 防御性清除：确保清空时标志干净
        # 清理残留的补充消息
        from agent.runner import drain_supplements
        drain_supplements()
        store = await get_message_store()
        # /clear：先跑 force 整理（entity→dream→journal，skip_compress），阻塞；超时降级为 clear-messages-only
        if force_tidy:
            try:
                await asyncio.wait_for(
                    _tidy_context_impl(
                        {"session_id": "default", "mode": "force", "skip_compress": True},
                        chat_lock_already_held=True,  # clear_chat 已持有 _chat_lock，防 asyncio.Lock 不可重入死锁
                    ),
                    timeout=600.0,  # 兜底：单个子 agent LLM 卡死时不永久占锁
                )
            except asyncio.TimeoutError:
                logger.warning("[clear_chat] tidy 600s timeout, clear-messages-only (orphan subagent thread self-heals)")
            except Exception as e:
                logger.warning(f"[clear_chat] run_tidy_before failed, proceed to clear: {e}")
        count = await store.clear_messages()

        # 重置 runner 的所有状态
        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if runner:
            # 重置 handler 的工作记忆
            if runner.handler:
                runner.handler.reset_working_memory()
                runner.handler._last_prompt_tokens = 0
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
        # 重置游标文件（消息已清空，旧游标指向不存在的消息）
        await _reset_all_cursors()

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
            "mode": "sleep" | "force"
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
    # 加锁防止并发：手动触发和自动触发互斥，超时10秒避免死锁
    try:
        await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
    except TimeoutError:
        logger.warning("[Tidy] tidy_context skipped: tidy lock held by another operation")
        return {"status": "skipped", "reason": "lock_busy"}
    try:
        return await _tidy_context_impl(request)
    finally:
        _tidy_lock.release()


async def _tidy_context_impl(request: dict, chat_lock_already_held: bool = False):
    """tidy_context 的内部实现（不加锁，由调用方负责并发控制）。

    Args:
        chat_lock_already_held: 调用方已持有 _chat_lock 时传 True，
            跳过内部的 _chat_lock 获取和 ChatQueue pause/resume，
            避免自死锁（asyncio.Lock 不可重入）。
    """
    # 压缩前状态占位：finally 判定是否实际压缩（try 早期失败/取消时保持默认 -1/None）
    _msgs_before = -1  # 压缩前消息数（finally 判定是否实际压缩；-1 表示 try 早期未读到）
    _store_ref = None  # try 内 store 引用（finally 复用，避免二次 get_message_store）
    # request 支持可选键 skip_compress: True 时跳过 force 模式的 context-manager 压缩
    # （用于 /clear 场景：只做内容提炼+梦境进化+日志记录，不压缩）
    session_id = request.get("session_id", "default")
    mode = request.get("mode", "sleep")
    stop_aware = mode == "force"  # 阶段间协作式停止：仅 force 模式（用户主动 /compact /clear）响应；sleep 模式（睡眠整理）不受停止按钮影响（程序任务隔离）

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
        from agent.subagent import call_subagent_with_auto_answer

        from niu_api.chat import get_or_create_runner

        runner = get_or_create_runner()
        if not runner:
            logger.warning("[Tidy] Runner not initialized")
            return {"status": "error", "message": "Runner not initialized"}

        llm_config = runner.llm_config

        import json
        from pathlib import Path

        # 读取三游标（UUID 基准）
        entity_cursor_path = Path.home() / ".niu" / "last_entity_extract.json"
        last_entity_extract_id = ""
        if entity_cursor_path.exists():
            try:
                cursor_data = json.loads(entity_cursor_path.read_text(encoding="utf-8"))
                last_entity_extract_id = cursor_data.get("last_entity_extract_id", "")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read entity cursor: {e}")

        dream_cursor_path = Path.home() / ".niu" / "last_dream_evolve.json"
        last_dream_evolve_id = ""
        if dream_cursor_path.exists():
            try:
                cursor_data = json.loads(dream_cursor_path.read_text(encoding="utf-8"))
                # 兼容旧格式（idx-based）和新格式（UUID-based）
                last_dream_evolve_id = cursor_data.get("last_dream_evolve_id", "")
                if not last_dream_evolve_id:
                    # 旧格式 fallback：last_message_idx → 留空，全量处理
                    logger.info("[Tidy] Old idx-based cursor detected, will do full processing")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read dream cursor: {e}")

        compress_cursor_path = Path.home() / ".niu" / "last_compress.json"
        last_compress_id = ""
        if compress_cursor_path.exists():
            try:
                cursor_data = json.loads(compress_cursor_path.read_text(encoding="utf-8"))
                last_compress_id = cursor_data.get("last_compress_id", "")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read compress cursor: {e}")

        journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
        last_journal_id = ""
        if journal_cursor_path.exists():
            try:
                cursor_data = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                last_journal_id = cursor_data.get("last_journal_id", "")
            except Exception as e:
                logger.warning(f"[Tidy] Failed to read journal cursor: {e}")


        if mode == "sleep":
            # Sleep mode: entity-extractor (增量) → dream-evolver (增量) → context-manager (增量)

            # 1/3. entity-extractor（增量，history 逐条 + task 独立指令）
            entity_msg_ids = []
            # _build_incremental_msg_text 仅用于收集增量范围内的 msg_ids（游标推进用）
            _ = _build_incremental_msg_text(
                messages, last_entity_extract_id, entity_msg_ids, msg_tokens
            )
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_task_prompt = """以上是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，N 是 1-based 序号）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                # 构造增量 history：只含游标之后的消息（按 entity_msg_ids 过滤）
                _id_set = set(entity_msg_ids)
                entity_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                entity_history, entity_idx_to_id = _build_plain_history(entity_incremental_msgs)

                def run_entity_extractor():
                    return call_subagent_with_auto_answer(
                        agent_name="entity-extractor",
                        task=entity_task_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=entity_history,
                        context_fifo_threshold=-1,  # FIFO 保底：首轮裁剪到 75%，防止溢出死循环
                    )

                entity_result = await asyncio.to_thread(run_entity_extractor)
                if stop_aware and is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] entity-extractor result: {entity_result[:200]}")

                # 游标推进：overflow/incomplete→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(entity_result) or _is_subagent_incomplete(entity_result) or _is_subagent_failure(entity_result):
                    if _is_subagent_incomplete(entity_result):
                        logger.warning(f"[Tidy] entity-extractor incomplete ({_incomplete_reason(entity_result)}) — cursor not advanced")
                    else:
                        overflow_info = _extract_overflow_info(entity_result)
                        logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow/incomplete 时游标不动，下次重跑相同范围
                else:
                    _processed_idx = _parse_processed_up_to(entity_result)
                    if _processed_idx is not None and _processed_idx in entity_idx_to_id:
                        new_entity_id = entity_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Entity cursor advanced per processed_up_to={_processed_idx} -> {new_entity_id}")
                    elif entity_msg_ids:
                        new_entity_id = entity_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Entity cursor fallback to range end: {new_entity_id}")
                    else:
                        new_entity_id = last_entity_extract_id
                # 校验游标
                if new_entity_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_entity_id not in fresh_ids:
                        logger.warning(f"[Tidy] Entity cursor {new_entity_id} deleted by sub-agent, reverting to {last_entity_extract_id}")
                        new_entity_id = last_entity_extract_id
                        if new_entity_id and new_entity_id not in fresh_ids:
                            new_entity_id = ""

                if new_entity_id:
                    _write_cursor_with_lock(entity_cursor_path, {
                        "last_entity_extract_id": new_entity_id,
                        "last_entity_extract_at": datetime.now().isoformat(),
                    })
                    logger.info(f"[Tidy] entity cursor updated: last_entity_extract_id={new_entity_id}")
            else:
                logger.info("[Tidy] entity-extractor: no new messages since cursor")

            # 2/3. dream-evolver（增量 task 方式）
            # 串行执行：重新获取消息列表（Entity 可能已修改 DB）
            messages = await store.get_messages()
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
            dream_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_task_prompt = """对以上消息中涉及的实体进行精加工（精简描述、建时间链、关联脑区、用户画像关联），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history
                _id_set = set(dream_msg_ids)
                dream_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                dream_history, dream_idx_to_id = _build_plain_history(dream_incremental_msgs)

                def run_dream_evolver():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=dream_task_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=dream_history,
                        context_fifo_threshold=-1,  # FIFO 保底
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver)
                if stop_aware and is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")
                # 补全会话日期链（只补边/断边，不建实体；to_thread 避免阻塞主事件循环，方法内部已容错）
                await asyncio.to_thread(runner._ensure_session_chain)

                # 游标推进：overflow/incomplete→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(dream_result) or _is_subagent_incomplete(dream_result) or _is_subagent_failure(dream_result):
                    if _is_subagent_incomplete(dream_result):
                        logger.warning(f"[Tidy] dream-evolver incomplete ({_incomplete_reason(dream_result)}) — cursor not advanced")
                    else:
                        overflow_info = _extract_overflow_info(dream_result)
                        logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow/incomplete 时游标不动，下次重跑相同范围
                else:
                    _processed_idx = _parse_processed_up_to(dream_result)
                    if _processed_idx is not None and _processed_idx in dream_idx_to_id:
                        new_dream_id = dream_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                    elif dream_msg_ids:
                        new_dream_id = dream_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Dream cursor fallback to range end: {new_dream_id}")
                    else:
                        new_dream_id = last_dream_evolve_id
                # 校验游标
                if new_dream_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_dream_id not in fresh_ids:
                        logger.warning(f"[Tidy] Dream cursor {new_dream_id} deleted by sub-agent, reverting to {last_dream_evolve_id}")
                        new_dream_id = last_dream_evolve_id
                        if new_dream_id and new_dream_id not in fresh_ids:
                            new_dream_id = ""
                if new_dream_id:
                    _write_cursor_with_lock(dream_cursor_path, {
                        "last_dream_evolve_id": new_dream_id,
                        "last_evolve_at": datetime.now().isoformat(),
                    })
                    logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")
            else:
                logger.info("[Tidy] dream-evolver: no new messages since cursor")
                new_dream_id = last_dream_evolve_id

            # 2.5/3. journal-agent（sleep 模式，仅 usage >= 50% 时调用）
            if usage_percent >= 50:
                # 重新获取消息列表（Dream 可能已修改 DB）
                messages = await store.get_messages()
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

                new_journal_id = last_journal_id
                journal_msg_ids = []
                _ = _build_incremental_msg_text(
                    messages, last_journal_id, journal_msg_ids, msg_tokens
                )
                logger.info(f"[Tidy] Sleep: starting journal-agent ({len(journal_msg_ids)} incremental messages)")

                if journal_msg_ids:
                    journal_task_prompt = _build_journal_task()
                    # 构造增量 history
                    _id_set = set(journal_msg_ids)
                    journal_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                    journal_history, journal_idx_to_id = _build_plain_history(journal_incremental_msgs)

                    def run_journal_agent():
                        return call_subagent_with_auto_answer(
                            agent_name="journal-agent",
                            task=journal_task_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            history=journal_history,
                            context_fifo_threshold=-1,  # FIFO 保底
                        )

                    journal_result = await asyncio.to_thread(run_journal_agent)
                    if stop_aware and is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] journal-agent result: {journal_result[:200]}")

                    # 游标推进：overflow/incomplete→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                    if _is_subagent_overflow(journal_result) or _is_subagent_incomplete(journal_result) or _is_subagent_failure(journal_result):
                        if _is_subagent_incomplete(journal_result):
                            logger.warning(f"[Tidy] journal-agent incomplete ({_incomplete_reason(journal_result)}) — cursor not advanced")
                        else:
                            overflow_info = _extract_overflow_info(journal_result)
                            logger.warning(f"[Tidy] journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow/incomplete 时游标不动，下次重跑相同范围
                    else:
                        _processed_idx = _parse_processed_up_to(journal_result)
                        if _processed_idx is not None and _processed_idx in journal_idx_to_id:
                            new_journal_id = journal_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Journal cursor advanced per processed_up_to={_processed_idx} -> {new_journal_id}")
                        elif journal_msg_ids:
                            new_journal_id = journal_msg_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Journal cursor fallback to range end: {new_journal_id}")
                        else:
                            new_journal_id = last_journal_id

                    # 校验游标
                    if new_journal_id:
                        fresh_msgs = await store.get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_journal_id not in fresh_ids:
                            logger.warning(f"[Tidy] Journal cursor {new_journal_id} deleted, reverting to {last_journal_id}")
                            new_journal_id = last_journal_id
                            if new_journal_id and new_journal_id not in fresh_ids:
                                new_journal_id = ""

                    if new_journal_id:
                        _write_cursor_with_lock(journal_cursor_path, {
                            "last_journal_id": new_journal_id,
                            "last_journal_at": datetime.now().isoformat(),
                        })
                        logger.info(f"[Tidy] Journal cursor updated: last_journal_id={new_journal_id}")
                else:
                    logger.info("[Tidy] journal-agent: no new messages since cursor")
            else:
                logger.info(f"[Tidy] journal-agent: skipped (usage {usage_percent:.1f}% < 50%)")

            # 3/3. context-manager（增量 task 方式，保护范围 [compress_cursor, dream_cursor_new]）
            # 串行执行：重新获取消息列表（Dream 可能已修改 DB）
            messages = await store.get_messages()
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
            # 读取保护数量配置
            protect_recent_count = _read_protect_recent_count()

            # 模式二：始终全量传入（无游标机制），模式一：增量范围
            _is_mode2 = usage_percent >= 50
            _compress_cursor = "" if _is_mode2 else last_compress_id
            _end_cursor = None if _is_mode2 else new_dream_id
            compress_msg_ids = []
            compress_history: list[dict] = []  # 模式二专用（替代 compress_msg_text）
            if _is_mode2:
                # 模式二：构造 history 列表（每条 message 加 idx 前缀），避免单条 user message 超限
                compress_history, _ = _build_compress_history(
                    messages, msg_tokens,
                    out_msg_ids=compress_msg_ids,
                    protect_recent=protect_recent_count,
                    exclude_protected=True,
                )
                compress_msg_text = ""  # 模式二不用序列化文本
            else:
                # 模式一：保持原序列化文本逻辑
                compress_msg_text = _build_incremental_msg_text(
                    messages, _compress_cursor, compress_msg_ids, msg_tokens,
                    end_cursor_id=_end_cursor, protect_recent=protect_recent_count,
                    exclude_protected=True
                )

            if not _is_mode2:
                # 模式一：限制增量范围的 token 总量，避免截断砍掉第三份（近期）消息
                _compress_window = int(_read_context_window_tokens() * 0.4)
                if compress_msg_text and _estimate_text_tokens(compress_msg_text) > _compress_window:
                    compress_msg_text = _truncate_preserving_tail(compress_msg_text, _compress_window)
                    _visible_ids = re.findall(r'\[id:([a-f0-9-]+)\]', compress_msg_text)
                    _visible_set = set(_visible_ids)
                    compress_msg_ids = [mid for mid in compress_msg_ids if mid in _visible_set]
            compress_mode = "模式二：睡眠整理（半破坏性）" if _is_mode2 else "模式一：睡眠整理（非破坏性）"
            _skip_compress = False
            # 接近强制压缩阈值时跳过睡眠压缩，避免与强制压缩并发冲突
            # 阈值 = warningThreshold - 0.1（默认0.8-0.1=0.7，即70%以上跳过）
            _warning_threshold = _read_warning_threshold()
            _skip_compress_threshold = (_warning_threshold - 0.1) * 100
            if usage_percent >= _skip_compress_threshold:
                logger.info(f"[Tidy] Sleep: usage {usage_percent:.1f}% >= skip threshold {_skip_compress_threshold:.0f}% (warningThreshold-0.1), skipping compression — will be handled by force mode")
                _skip_compress = True
            # 模式二量化目标：基于 compressTargetTokens（绝对值）计算动态目标（提前计算，决定是否跳过）
            _compress_target = ""
            if _skip_compress:
                pass  # 接近强制阈值，跳过所有压缩
            elif _is_mode2:
                target_tokens = _read_compress_target_tokens()
                suggest_release = max(display_tokens - target_tokens, 0)
                if suggest_release == 0:
                    # 当前已在目标范围内，不需要压缩
                    logger.info("[Tidy] Mode-2: already at target, skipping compression")
                    _skip_compress = True
                elif suggest_release < int(display_tokens * 0.05):
                    # 释放量太小（<5%），不值得压缩一轮，跳过
                    logger.info(f"[Tidy] Mode-2: suggest_release {suggest_release} < 5%, skipping compression")
                    _skip_compress = True
                # 模式二 task prompt 由 _build_mode2_prompt 构造（瘦身版：方法论在 system，仅输出 keep=/update= 两行指令）
                # 模式一/二：游标均由程序自动推进，不需要报告指令
            logger.info(f"[Tidy] Sleep: usage={usage_percent:.1f}%, selecting {compress_mode}")

            new_compress_id = last_compress_id
            if compress_msg_ids and not _skip_compress:
                # 构建保护消息 UUID 列表（含所有消息类型：user/assistant/tool，从 protect_start 到末尾）
                # 直接从完整 messages 列表计算，不依赖截断后的 compress_msg_ids
                # 这样即使截断移除了第三份（近期）消息，受保护消息的 ID 仍然完整
                _protect_start = _find_protected_range(messages, protect_recent_count)
                # Protect all messages (including tool) from protect_start to end
                protected_ids = [getattr(messages[i], "id", "") or "" for i in range(_protect_start, len(messages))]

                if _is_mode2:
                    compress_plan_path = os.path.expanduser("~/.niu/compress_plan_mode2.json")
                    if os.path.exists(compress_plan_path):
                        try:
                            os.remove(compress_plan_path)
                        except OSError:
                            pass

                    # 模式二 task prompt 由 _build_mode2_prompt 构造（瘦身版：禁止报告，直接输出两行）
                    prompt = ""
                else:
                    prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}消息列表（已排除受保护消息）：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。"""

                # 截断 task 防止子Agent超限 + 子Agent调用 + 结果处理
                if _is_mode2:
                    # === 模式二：单次调用 + 应急清空（不重试） ===
                    # 压缩 LLM 配置：按知识图谱（lightrag 段）用户配置 + max_tokens（输出预算）
                    llm_config_with_max = _build_compress_llm_config()

                    # 单次调用（不重试，截断时走应急清空）；复用上方已读的 target_tokens
                    prompt = _build_mode2_prompt(display_tokens, target_tokens, usage_percent, compress_history)

                    def run_context_manager_mode2():
                        return call_subagent_with_auto_answer(
                            agent_name="context-manager",
                            task=prompt,
                            llm_config=llm_config_with_max,
                            mcp_client=None,
                            context_fifo_threshold=-1,  # FIFO 保底
                            history=compress_history,  # 直接传 messages 列表，避免单条 user message 超限
                            bypass_at_prefix=True,  # 一轮出方案：绕过@前缀拦截，禁止追问第二轮（防上下文溢出）
                        )

                    compress_result = await asyncio.to_thread(run_context_manager_mode2)
                    _halved_msg_ids = None  # 降级砍半的前半段 msg_ids（正常路径为 None）

                    if stop_aware and is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}

                    # SUBAGENT_ERROR: context-manager LLM 错误，跳过不删消息
                    if compress_result and compress_result.startswith("SUBAGENT_ERROR:"):
                        error_msg = compress_result[len("SUBAGENT_ERROR:"):]
                        logger.warning(f"[Compact] Mode-2: context-manager LLM error: {error_msg}")
                        return {"status": "skipped", "mode": "sleep", "reason": f"LLM error: {error_msg}"}

                    # 截断时触发三级降级（关思考链→砍半消息→报失败）
                    if compress_result and compress_result.startswith("COMPACT_TRUNCATED:"):
                        logger.warning("[Compact] Mode-2 output truncated, starting degradation")
                        result_str, actual_msg_ids, halved_msg_ids = await asyncio.to_thread(
                            _compact_with_degradation_sync,
                            agent_name="context-manager",
                            prompt=prompt,
                            compress_history=compress_history,
                            compress_msg_ids=compress_msg_ids,
                            llm_config=llm_config_with_max,
                            prompt_builder=_build_mode2_prompt,
                            prompt_builder_kwargs={
                                "display_tokens": display_tokens,
                                "compress_target_tokens": target_tokens,
                                "usage_percent": usage_percent,
                                "compress_history": compress_history,
                            },
                            stop_aware=(mode == "force"),
                            call_fn=call_subagent_with_auto_answer,
                        )
                        if result_str is None:
                            return {"status": "skipped", "mode": "sleep",
                                    "reason": "compress failed: output truncated after all degradation steps"}
                        # 降级成功，用返回值替代 compress_result
                        compress_result = result_str
                        compress_msg_ids = actual_msg_ids
                        _halved_msg_ids = halved_msg_ids

                    # 未完成终止（/stop、轮次耗尽等）：跳过压缩，保持调用方游标不动
                    if _is_subagent_incomplete(compress_result):
                        logger.warning(f"[Compact] Mode-2: context-manager incomplete ({_incomplete_reason(compress_result)}), compression skipped")
                        return {"status": "skipped", "mode": "sleep",
                                "reason": f"context-manager incomplete ({_incomplete_reason(compress_result)})"}

                    # 正常返回，剥离 <analysis> 草稿块（在解析前）
                    logger.info(f"[Tidy] Mode-2: context-manager completed, length={len(compress_result)}")
                    compress_result = _strip_analysis(compress_result)

                    # 从 LLM content 解析序号格式压缩方案
                    _idx_to_id: dict[int, str] = {}
                    for _i, _mid in enumerate(compress_msg_ids):
                        _idx_to_id[_i + 1] = _mid

                    keep_idxs: set[int] = set()
                    update_list: list[tuple[int, str]] = []
                    for line in compress_result.splitlines():
                        line = line.strip()
                        if line.lower().startswith('keep='):
                            keep_idxs = _parse_idx_list(line.split('=', 1)[1].strip())
                        elif line.lower().startswith('update='):
                            update_str = line.split('=', 1)[1].strip()
                            if update_str:
                                for part in update_str.split(';'):
                                    part = part.strip()
                                    if '|' in part:
                                        idx_str, content = part.split('|', 1)
                                        try:
                                            _c = content.strip()
                                            if not _c.startswith('[摘要]') and not _c.startswith('[合并]'):
                                                _c = f'[摘要] {_c}'
                                            update_list.append((int(idx_str.strip()), _c))
                                        except ValueError:
                                            pass

                    if not keep_idxs:
                        logger.error("[Tidy] Mode-2: No keep= line found in LLM response, compression skipped")
                    else:
                        all_idxs = set(_idx_to_id.keys())
                        delete_idxs = all_idxs - keep_idxs
                        deletes = [_idx_to_id[i] for i in sorted(delete_idxs) if i in _idx_to_id]
                        # 砍半掉的前半段 msg_ids 加入删除列表
                        if _halved_msg_ids:
                            deletes.extend(_halved_msg_ids)
                        for idx, _ in update_list:
                            if idx not in _idx_to_id:
                                logger.warning(f"[Compact] Mode-2 LLM returned out-of-range update idx {idx}, silently dropped")
                        updates = [
                            {"message_id": _idx_to_id[idx], "content": content}
                            for idx, content in update_list if idx in _idx_to_id
                        ]
                        logger.info(f"[Tidy] Mode-2: Plan parsed: {len(deletes)} deletes, {len(updates)} updates (keep={len(keep_idxs)})")

                        # 安全协议：pause + acquire chat_lock + 等待worker空闲
                        from niu_api.chat_queue import get_chat_queue
                        _q = get_chat_queue()
                        _q.pause()

                        from niu_api.chat import _chat_lock
                        _chat_lock_acquired = False
                        try:
                            await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
                            _chat_lock_acquired = True
                        except TimeoutError:
                            logger.warning("[Tidy] Mode-2: chat_lock 60s timeout, aborting execution")

                        if not _chat_lock_acquired:
                            _q.resume()
                            raise RuntimeError("chat_lock timeout")

                        if _q._processing and _q._processing_done.is_set():
                            pass
                        elif _q._processing:
                            try:
                                await asyncio.wait_for(_q._processing_done.wait(), timeout=30.0)
                            except TimeoutError:
                                logger.warning("[Tidy] Mode-2: ChatQueue processing timeout, aborting execution")
                                if _chat_lock_acquired:
                                    _chat_lock.release()
                                _q.resume()
                                raise RuntimeError("ChatQueue processing timeout") from None

                        try:
                            fresh_messages = await store.get_messages()
                            existing_ids = {getattr(m, "id", "") for m in fresh_messages}

                            valid_deletes = [mid for mid in deletes if mid in existing_ids]
                            valid_deletes = list(dict.fromkeys(valid_deletes))
                            valid_updates = [u for u in updates if u.get("message_id") and u["message_id"] in existing_ids]

                            cursor_ids_set = {cid for cid in [new_entity_id, new_dream_id] if cid}
                            valid_deletes = [mid for mid in valid_deletes if mid not in cursor_ids_set]
                            valid_updates = [u for u in valid_updates if u.get("message_id", "") not in cursor_ids_set]

                            if new_dream_id:
                                dream_boundary_idx = -1
                                for i, m in enumerate(fresh_messages):
                                    if (getattr(m, "id", "") or "") == new_dream_id:
                                        dream_boundary_idx = i
                                        break
                                if dream_boundary_idx >= 0:
                                    post_dream_ids = {getattr(m, "id", "") for m in fresh_messages[dream_boundary_idx + 1:]}
                                    unsafe_deletes = [mid for mid in valid_deletes if mid in post_dream_ids]
                                    unsafe_updates = [u for u in valid_updates if u.get("message_id", "") in post_dream_ids]
                                    if unsafe_deletes:
                                        logger.warning(f"[Tidy] Mode-2: Protecting {len(unsafe_deletes)} post-dream messages from deletion")
                                        valid_deletes = [mid for mid in valid_deletes if mid not in post_dream_ids]
                                    if unsafe_updates:
                                        logger.warning(f"[Tidy] Mode-2: Protecting {len(unsafe_updates)} post-dream messages from content replacement")
                                        valid_updates = [u for u in valid_updates if u.get("message_id", "") not in post_dream_ids]

                            protect_recent_count = _read_protect_recent_count()
                            # 防御 UUID 幻觉：PROTECTED 消息已从输入中排除，但 LLM 可能幻觉出其 UUID
                            protected_set: set[str] = set()
                            if protect_recent_count > 0:
                                _protect_start = _find_protected_range(fresh_messages, protect_recent_count)
                                protected_set = {getattr(fresh_messages[i], "id", "") or "" for i in range(_protect_start, len(fresh_messages))}
                                valid_deletes = [mid for mid in valid_deletes if mid not in protected_set]
                                valid_updates = [u for u in valid_updates if u.get("message_id", "") not in protected_set]

                            update_ids = {u.get("message_id", "") for u in valid_updates}
                            overlap_ids = update_ids & set(valid_deletes)
                            if overlap_ids:
                                logger.warning(f"[Tidy] Mode-2: Removing {len(overlap_ids)} IDs from deletes that also appear in updates")
                                valid_deletes = [mid for mid in valid_deletes if mid not in overlap_ids]

                            _cascade_protected = cursor_ids_set | (protected_set if protect_recent_count > 0 else set())
                            cascade_del = _cascade_tool_chain_deletes(fresh_messages, valid_deletes, protected_ids=_cascade_protected)
                            valid_deletes = cascade_del.delete_ids
                            dangling_tc_cleanups = cascade_del.dangling_cleanups
                            cascade_upd = _cascade_tool_chain_updates(fresh_messages, valid_updates)
                            valid_updates = cascade_upd.updates
                            cascade_delete_ids = cascade_upd.cascade_delete_ids
                            if cascade_delete_ids:
                                existing = set(valid_deletes)
                                for cid in cascade_delete_ids:
                                    if cid not in existing:
                                        valid_deletes.append(cid)
                                        existing.add(cid)

                            _post_update_ids = {u.get("message_id", "") for u in valid_updates}
                            _post_overlap = _post_update_ids & set(valid_deletes)
                            if _post_overlap:
                                logger.warning(f"[Tidy] Mode-2: Cascade created delete/update overlap: {_post_overlap}")
                                valid_deletes = [mid for mid in valid_deletes if mid not in _post_overlap]

                            if dangling_tc_cleanups:
                                for cleanup in dangling_tc_cleanups:
                                    mid = cleanup["message_id"]
                                    dangling_ids = cleanup["dangling_tc_ids"]
                                    m = next((m for m in fresh_messages if getattr(m, "id", "") == mid), None)
                                    if m and getattr(m, "tool_calls", None):
                                        tcs = m.tool_calls
                                        if isinstance(tcs, str):
                                            tcs = json.loads(tcs)
                                        valid_tcs = [tc for tc in tcs if tc.get("id", "") not in dangling_ids]
                                        if valid_tcs:
                                            await _clean_dangling_tool_calls(store, mid, valid_tcs)
                                        else:
                                            await store.update_message(mid, getattr(m, "content", "") or "", clear_tool_calls=True)
                                        logger.info(f"[Tidy] Mode-2: Cleaned {len(dangling_ids)} dangling tool_calls from protected assistant {mid}")

                            if valid_deletes:
                                del_result = await store.delete_messages_by_ids(valid_deletes)
                                logger.info(f"[Tidy] Mode-2: Deleted {del_result.get('deleted_count', 0)} messages, freed {del_result.get('freed_tokens', 0)} tokens")

                            for upd in valid_updates:
                                mid = upd.get("message_id", "")
                                content = upd.get("content", "")
                                if mid and content:
                                    clear_tc = upd.get("clear_tool_calls", False)
                                    ok = await store.update_message(message_id=mid, content=content, clear_tool_calls=clear_tc)
                                    if ok:
                                        logger.info(f"[Tidy] Mode-2: Updated message {mid}")
                                    else:
                                        logger.warning(f"[Tidy] Mode-2: Failed to update message {mid}")

                            await _cleanup_orphan_tool_messages(store)
                            logger.info(f"[Tidy] Mode-2: Compression plan executed: {len(valid_deletes)} deletes, {len(valid_updates)} updates")
                        finally:
                            if _chat_lock_acquired:
                                _chat_lock.release()
                            _q.resume()

                    # 模式二完成后清空压缩游标：模式二全量重组了消息列表，旧游标语义失效
                    # 清空后下次模式一自然全量处理，行为明确且一致
                    _write_cursor_with_lock(compress_cursor_path, {
                        "last_compress_id": "",
                        "last_compress_at": datetime.now().isoformat(),
                    })
                    logger.info("[Tidy] Mode-2: Cleared compress cursor (full-range reorg invalidated old cursor)")
                else:
                    # === 模式一：原有逻辑完整保留 ===
                    context_window_for_truncate = _read_context_window_tokens()
                    safe_tokens = int(context_window_for_truncate * 0.6)
                    truncated_prompt = _truncate_task_for_subagent(prompt, safe_tokens)

                    def run_context_manager():
                        return call_subagent_with_auto_answer(
                            agent_name="context-manager",
                            task=truncated_prompt,
                            llm_config=_build_compress_llm_config(),
                            mcp_client=None,
                            context_fifo_threshold=-1,  # FIFO 保底
                        )

                    cm_result = await asyncio.to_thread(run_context_manager)
                    if stop_aware and is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] context-manager result: {cm_result[:200]}")

                    # 游标自动推进：成功→推进到范围内仍存在的最后一条，overflow/incomplete→不动
                    fresh_ids = None
                    if _is_subagent_overflow(cm_result) or _is_subagent_incomplete(cm_result) or _is_subagent_failure(cm_result):
                        if _is_subagent_incomplete(cm_result):
                            logger.warning(f"[Tidy] context-manager incomplete ({_incomplete_reason(cm_result)}) — cursor not advanced")
                        else:
                            overflow_info = _extract_overflow_info(cm_result)
                            logger.warning(f"[Tidy] context-manager overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow/incomplete 时游标不动
                    else:
                        # 不盲取 compress_msg_ids[-1]（可能被 context-manager 删除），
                        # 而是重新读取 DB，取范围内仍存在的最后一条
                        fresh_msgs = await store.get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        surviving = [mid for mid in compress_msg_ids if mid in fresh_ids]
                        new_compress_id = surviving[-1] if surviving else last_compress_id
                        logger.info(f"[Tidy] Compress cursor auto-advanced to: {new_compress_id}")

                    # 校验游标指向的消息仍存在（last_compress_id 可能已失效）
                    if new_compress_id:
                        if fresh_ids is None:
                            fresh_msgs = await store.get_messages()
                            fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_compress_id not in fresh_ids:
                            logger.warning(f"[Tidy] Compress cursor {new_compress_id} not in DB, reverting to {last_compress_id}")
                            new_compress_id = last_compress_id
                            if new_compress_id and new_compress_id not in fresh_ids:
                                new_compress_id = ""

                    compress_integrity_ok = True
                    if protected_ids:
                        try:
                            post_msgs = await store.get_messages()
                            post_ids = {getattr(m, "id", "") for m in post_msgs}
                            for pid in protected_ids:
                                if pid not in post_ids:
                                    logger.error(f"[Tidy] PROTECTED message {pid} missing after compress! Blocking cursor advance.")
                                    compress_integrity_ok = False
                                    break
                        except Exception as e:
                            logger.warning(f"[Tidy] Failed to verify protected messages: {e}")
                            compress_integrity_ok = False

                    # 工具链完整性验证：迭代收敛，检测并修复孤立 tool 消息和悬空 tool_calls
                    # 清除 tool_calls 后可能产生新孤立 tool 消息，需要反复扫描直到收敛
                    try:
                        for _round in range(5):  # 最多 5 轮收敛
                            post_msgs = await store.get_messages()
                            # 收集所有 assistant tool_calls 的 id
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
                            # 收集所有 tool 消息的 tool_call_id + 孤立 tool 消息
                            _tool_response_ids: set[str] = set()
                            _orphan_tool_mids: list[str] = []
                            for m in post_msgs:
                                if getattr(m, "role", "") == "tool":
                                    tc_call_id = getattr(m, "tool_call_id", "") or ""
                                    if tc_call_id:
                                        _tool_response_ids.add(tc_call_id)
                                        if tc_call_id not in _valid_tc_ids:
                                            _orphan_tool_mids.append(getattr(m, "id", ""))
                            # 检测悬空 tool_calls
                            _dangling_tc_ids: set[str] = set()
                            for tc_id in _valid_tc_ids:
                                if tc_id not in _tool_response_ids:
                                    _dangling_tc_ids.add(tc_id)

                            if not _orphan_tool_mids and not _dangling_tc_ids:
                                break  # 收敛，无需继续

                            # 修复：删除孤立 tool 消息
                            if _orphan_tool_mids:
                                logger.warning(f"[Tidy] Mode-1 integrity round {_round+1}: deleting {len(_orphan_tool_mids)} orphan tool messages")
                                await store.delete_messages_by_ids(_orphan_tool_mids)
                            # 修复：清除悬空 tool_calls
                            if _dangling_tc_ids:
                                for m in post_msgs:
                                    if getattr(m, "role", "") == "assistant":
                                        tcs = getattr(m, "tool_calls", None)
                                        if tcs:
                                            try:
                                                if isinstance(tcs, str):
                                                    tcs = json.loads(tcs)
                                                valid_tcs = [tc for tc in tcs if tc.get("id", "") not in _dangling_tc_ids]
                                                if len(valid_tcs) < len(tcs):
                                                    mid = getattr(m, "id", "")
                                                    if valid_tcs:
                                                        await _clean_dangling_tool_calls(store, mid, valid_tcs)
                                                        logger.warning(f"[Tidy] Mode-1 integrity round {_round+1}: cleaned {len(tcs) - len(valid_tcs)} dangling tool_calls from {mid}")
                                                    else:
                                                        await store.update_message(mid, getattr(m, "content", "") or "",
                                                                                   clear_tool_calls=True)
                                                        logger.warning(f"[Tidy] Mode-1 integrity round {_round+1}: cleared all tool_calls from {mid}")
                                            except (json.JSONDecodeError, TypeError):
                                                pass
                    except Exception as e:
                        logger.warning(f"[Tidy] Mode-1 tool chain integrity check failed: {e}")

                    # 模式一：压缩后统一清理孤立 tool + 空壳 assistant 消息
                    await _cleanup_orphan_tool_messages(store)

                    # 模式一：推进压缩游标
                    if new_compress_id:
                        if compress_integrity_ok:
                            _write_cursor_with_lock(compress_cursor_path, {
                                "last_compress_id": new_compress_id,
                                "last_compress_at": datetime.now().isoformat(),
                            })
                            logger.info(f"[Tidy] Compress cursor updated: last_compress_id={new_compress_id}")
                        else:
                            logger.warning("[Tidy] Skipping cursor advance due to protected message integrity failure")
            else:
                if _skip_compress:
                    if usage_percent >= _skip_compress_threshold:
                        logger.info(f"[Tidy] context-manager: skipped (usage {usage_percent:.1f}% >= skip threshold {_skip_compress_threshold:.0f}%, waiting for force mode)")
                    else:
                        logger.info("[Tidy] context-manager: skipped (suggest_release below threshold or already at target)")
                else:
                    logger.info("[Tidy] context-manager: no messages to process")

            return {"status": "ok", "mode": "sleep", "tokens_before": display_tokens}

        elif mode == "force":
            # Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩
            logger.info("[Tidy] Force mode: starting entity-extractor (full processing)")
            # 计算 PROTECTED 消息 ID 集合（最近 N 条 user/assistant，与 context-manager 对齐）
            # 用于 entity force 方案 A：排除最近 PROTECTED 条防止 overflow 死循环（详见 Architecture §6）
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and messages:
                _protect_start = _find_protected_range(messages, _force_protect_recent_count)
                _force_protected_ids = {getattr(messages[i], "id", "") or "" for i in range(_protect_start, len(messages))}

            # 1/3. entity-extractor（全量 history 逐条 + task 独立指令，cursor 传空 = 全量）
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, "", entity_force_msg_ids, msg_tokens
            )
            entity_force_prompt = """以上是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，1-based）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
            # 构造全量 history + idx_to_id 映射（force 模式 cursor 为空 = 全量）
            # 方案 A：排除 PROTECTED 消息（最近 N 条 user/assistant）防止 overflow 死循环（详见 Architecture §6）
            # _force_protected_ids 已在 Step 6.0 force 块顶部计算（与 context-manager protect_recent_count 对齐）
            entity_force_msgs_filtered = [m for m in messages if (getattr(m, "id", "") or "") not in _force_protected_ids]
            entity_force_history, entity_force_idx_to_id = _build_plain_history(entity_force_msgs_filtered)
            # 同步过滤 entity_force_msg_ids（游标推进兜底用，与 history 保持一致）
            entity_force_msg_ids = [getattr(m, "id", "") or "" for m in entity_force_msgs_filtered]

            def run_entity_extractor_force():
                return call_subagent_with_auto_answer(
                    agent_name="entity-extractor",
                    task=entity_force_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    history=entity_force_history,
                    context_fifo_threshold=-1,  # FIFO 保底
                )

            if entity_force_msg_ids:
                entity_result = await asyncio.to_thread(run_entity_extractor_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: entity-extractor completed, length={len(entity_result)}")

                if _is_subagent_overflow(entity_result) or _is_subagent_incomplete(entity_result) or _is_subagent_failure(entity_result):
                    if _is_subagent_incomplete(entity_result):
                        logger.warning(f"[Tidy] Force: entity-extractor incomplete ({_incomplete_reason(entity_result)}) — cursor not advanced")
                    else:
                        overflow_info = _extract_overflow_info(entity_result)
                        logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow/incomplete 时游标不动
                else:
                    _processed_idx = _parse_processed_up_to(entity_result)
                    if _processed_idx is not None and _processed_idx in entity_force_idx_to_id:
                        new_entity_id = entity_force_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Force: Entity cursor advanced per processed_up_to={_processed_idx} -> {new_entity_id}")
                    elif entity_force_msg_ids:
                        new_entity_id = entity_force_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Force: Entity cursor fallback to range end: {new_entity_id}")
                    else:
                        new_entity_id = last_entity_extract_id
                # 校验游标
                if new_entity_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_entity_id not in fresh_ids:
                        logger.warning(f"[Tidy] Force: Entity cursor {new_entity_id} deleted by sub-agent, reverting to {last_entity_extract_id}")
                        new_entity_id = last_entity_extract_id
                        if new_entity_id and new_entity_id not in fresh_ids:
                            new_entity_id = ""
                if new_entity_id:
                    _write_cursor_with_lock(entity_cursor_path, {
                        "last_entity_extract_id": new_entity_id,
                        "last_entity_extract_at": datetime.now().isoformat(),
                    })
            else:
                logger.info("[Tidy] Force mode: entity-extractor skipped, no messages")

            # 2/3. dream-evolver（增量 task 方式，force 模式也是增量）
            # 串行执行：重新获取消息列表
            messages = await store.get_messages()
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
            dream_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            new_dream_id = last_dream_evolve_id  # 默认保留旧游标，防止 overflow 时未定义
            if dream_force_msg_ids:
                dream_force_prompt = """对以上消息中涉及的实体进行精加工（精简描述、建时间链、关联脑区、用户画像关联），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history
                _id_set = set(dream_force_msg_ids)
                dream_force_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                def run_dream_evolver_force():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=dream_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=dream_force_history,
                        context_fifo_threshold=-1,  # FIFO 保底
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")
                # 补全会话日期链（只补边/断边，不建实体；to_thread 避免阻塞主事件循环，方法内部已容错）
                await asyncio.to_thread(runner._ensure_session_chain)

                # 游标推进：overflow/incomplete→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(dream_result) or _is_subagent_incomplete(dream_result) or _is_subagent_failure(dream_result):
                    if _is_subagent_incomplete(dream_result):
                        logger.warning(f"[Tidy] Force: Dream-evolver incomplete ({_incomplete_reason(dream_result)}) — cursor not advanced")
                    else:
                        overflow_info = _extract_overflow_info(dream_result)
                        logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow/incomplete 时游标不动
                else:
                    _processed_idx = _parse_processed_up_to(dream_result)
                    if _processed_idx is not None and _processed_idx in dream_force_idx_to_id:
                        new_dream_id = dream_force_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Force: Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                    elif dream_force_msg_ids:
                        new_dream_id = dream_force_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Force: Dream cursor fallback to range end: {new_dream_id}")
                    else:
                        new_dream_id = last_dream_evolve_id
            else:
                logger.info("[Tidy] Force: dream-evolver no incremental messages")
                new_dream_id = last_dream_evolve_id  # 无增量时保留旧游标，避免 UnboundLocalError

            # 校验游标
            if new_dream_id:
                fresh_msgs = await store.get_messages()
                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                if new_dream_id not in fresh_ids:
                    logger.warning(f"[Tidy] Force: Dream cursor {new_dream_id} deleted by sub-agent, reverting to {last_dream_evolve_id}")
                    new_dream_id = last_dream_evolve_id
                    if new_dream_id and new_dream_id not in fresh_ids:
                        new_dream_id = ""

            if new_dream_id:
                _write_cursor_with_lock(dream_cursor_path, {
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                })

            # 2.5/3. journal-agent（force 模式，始终调用）
            # 重新获取消息列表
            messages = await store.get_messages()
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

            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                journal_force_prompt = _build_journal_task()  # 纯指令，无参（含 processed_up_to 说明）
                # 构造增量 history
                _id_set = set(journal_force_msg_ids)
                journal_force_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                journal_force_history, journal_force_idx_to_id = _build_plain_history(journal_force_incremental_msgs)

                def run_journal_agent_force():
                    return call_subagent_with_auto_answer(
                        agent_name="journal-agent",
                        task=journal_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=journal_force_history,
                        context_fifo_threshold=-1,  # FIFO 保底
                    )

                journal_result = await asyncio.to_thread(run_journal_agent_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: journal-agent completed, length={len(journal_result)}")

                # 游标推进：overflow/incomplete→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(journal_result) or _is_subagent_incomplete(journal_result) or _is_subagent_failure(journal_result):
                    if _is_subagent_incomplete(journal_result):
                        logger.warning(f"[Tidy] Force: journal-agent incomplete ({_incomplete_reason(journal_result)}) — cursor not advanced")
                    else:
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] Force: journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # overflow/incomplete 时游标不动，下次重跑相同范围
                else:
                    _processed_idx = _parse_processed_up_to(journal_result)
                    if _processed_idx is not None and _processed_idx in journal_force_idx_to_id:
                        new_journal_id = journal_force_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Force: Journal cursor advanced per processed_up_to={_processed_idx} -> {new_journal_id}")
                    elif journal_force_msg_ids:
                        new_journal_id = journal_force_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Force: Journal cursor fallback to range end: {new_journal_id}")
                    else:
                        new_journal_id = last_journal_id

                # 校验游标
                if new_journal_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_journal_id not in fresh_ids:
                        logger.warning(f"[Tidy] Force: Journal cursor {new_journal_id} deleted, reverting to {last_journal_id}")
                        new_journal_id = last_journal_id
                        if new_journal_id and new_journal_id not in fresh_ids:
                            new_journal_id = ""

                if new_journal_id:
                    _write_cursor_with_lock(journal_cursor_path, {
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    })
                    logger.info(f"[Tidy] Force: Journal cursor updated: last_journal_id={new_journal_id}")
            else:
                logger.info("[Tidy] Force: journal-agent no incremental messages")

            # 3/3. context-manager 强制压缩（抽为嵌套闭包；skip_compress=True 时跳过）
            async def _compress_force():
                # 3/3. context-manager force prompt — 一轮 JSON 文件方案
                # 重新读取 compress 游标
                last_compress_id = ""
                if compress_cursor_path.exists():
                    try:
                        cdata = json.loads(compress_cursor_path.read_text(encoding="utf-8"))
                        last_compress_id = cdata.get("last_compress_id", "")
                    except Exception as e:
                        logger.warning(f"[Tidy] Failed to read compress cursor in force mode: {e}")

                target_tokens = _read_compress_target_tokens()
                compress_plan_path = os.path.expanduser("~/.niu/compress_plan.json")
                # 清理上次的残留计划文件
                if os.path.exists(compress_plan_path):
                    try:
                        os.remove(compress_plan_path)
                    except OSError:
                        pass  # Windows 文件锁，忽略

                protect_recent_count = _read_protect_recent_count()
                # 降级策略：允许外部传入更低的保护数量
                _force_protect_recent = request.get("force_protect_recent") if isinstance(request, dict) else None
                if _force_protect_recent is not None and isinstance(_force_protect_recent, int) and _force_protect_recent >= 1:
                    protect_recent_count = min(protect_recent_count, _force_protect_recent)
                    logger.info(f"[Tidy] Force: protect_recent_count degraded to {protect_recent_count} (from request)")

                # 使用统一的 _build_compress_history 构建（与模式二一致）
                _force_msg_ids = []
                _force_history, _ = _build_compress_history(
                    messages, msg_tokens,
                    out_msg_ids=_force_msg_ids,
                    protect_recent=protect_recent_count,
                    exclude_protected=True,
                )

                # 构建 idx→UUID 映射 + id→idx 反向映射（用于 prompt 和解析）
                _f_idx_to_id: dict[int, str] = {}
                _f_id_to_idx: dict[str, int] = {}
                for _i, _mid in enumerate(_force_msg_ids):
                    _f_idx_to_id[_i + 1] = _mid
                    _f_id_to_idx[_mid] = _i + 1

                # dream-evolver 安全边界 idx（排除后列表中的位置）
                if not new_dream_id:
                    _dream_idx_in_force = 0
                else:
                    _dream_idx_in_force = _f_id_to_idx.get(new_dream_id, len(_force_msg_ids))

                # 压缩 LLM 配置：按知识图谱（lightrag 段）用户配置 + max_tokens（输出预算）
                llm_config_with_max = _build_compress_llm_config()

                # 单次调用（不重试，截断时走应急清空）；复用上方已读的 target_tokens
                prompt = _build_force_prompt(
                    display_tokens, target_tokens, usage_percent,
                    _force_history, last_compress_id, _dream_idx_in_force
                )

                def run_context_manager_force():
                    return call_subagent_with_auto_answer(
                        agent_name="context-manager",
                        task=prompt,
                        llm_config=llm_config_with_max,
                        mcp_client=None,
                        context_fifo_threshold=-1,  # FIFO 保底
                        history=_force_history,  # 直接传 messages 列表，避免单条 user message 超限
                        bypass_at_prefix=True,  # 一轮出方案：绕过@前缀拦截，禁止追问第二轮（防上下文溢出）
                    )

                result = await asyncio.to_thread(run_context_manager_force)
                _force_halved_msg_ids = None  # 降级砍半的前半段 msg_ids
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                # SUBAGENT_ERROR: context-manager LLM 错误，跳过不删消息
                if result and result.startswith("SUBAGENT_ERROR:"):
                    error_msg = result[len("SUBAGENT_ERROR:"):]
                    logger.warning(f"[Compact] Force: context-manager LLM error: {error_msg}")
                    return {"status": "skipped", "mode": "force", "reason": f"LLM error: {error_msg}"}


                if result and result.startswith("COMPACT_TRUNCATED:"):
                    logger.warning("[Compact] Force output truncated, starting degradation")
                    result_str, actual_msg_ids, halved_msg_ids = await asyncio.to_thread(
                        _compact_with_degradation_sync,
                        agent_name="context-manager",
                        prompt=prompt,
                        compress_history=_force_history,
                        compress_msg_ids=_force_msg_ids,
                        llm_config=llm_config_with_max,
                        prompt_builder=_build_force_prompt,
                        prompt_builder_kwargs={
                            "display_tokens": display_tokens,
                            "compress_target_tokens": target_tokens,
                            "usage_percent": usage_percent,
                            "force_history": _force_history,
                            "last_compress_id": last_compress_id,
                            "dream_idx_in_force": _dream_idx_in_force,
                        },
                        stop_aware=True,
                        call_fn=call_subagent_with_auto_answer,
                    )
                    if result_str is None:
                        return {"status": "skipped", "mode": "force",
                                "reason": "compress failed: output truncated after all degradation steps"}
                    # 降级成功，用返回值替代 result
                    result = result_str
                    _force_msg_ids = actual_msg_ids
                    _force_halved_msg_ids = halved_msg_ids
                    # 重建 idx→UUID 映射（砍半后 msg_ids 变化，旧映射失效）
                    _f_idx_to_id = {}
                    for _i, _mid in enumerate(_force_msg_ids):
                        _f_idx_to_id[_i + 1] = _mid

                # 正常返回，剥离 <analysis> 草稿块（在解析前）
                logger.info(f"[Tidy] Force: context-manager completed, length={len(result)}")
                result = _strip_analysis(result)

                # 从 sub-agent 回复中解析压缩计划（idx 格式）
                new_compress_id = last_compress_id
                try:
                    keep_idxs: set[int] = set()
                    update_list: list[tuple[int, str]] = []
                    cursor_idx: int | None = None

                    for line in result.splitlines():
                        line = line.strip()
                        if line.lower().startswith("keep="):
                            keep_idxs = _parse_idx_list(line.split("=", 1)[1].strip())
                        elif line.lower().startswith("update="):
                            update_str = line.split("=", 1)[1].strip()
                            if update_str:
                                for part in update_str.split(";"):
                                    part = part.strip()
                                    if "|" in part:
                                        idx_str, content = part.split("|", 1)
                                        try:
                                            idx = int(idx_str.strip())
                                            _c = content.strip()
                                            if not _c.startswith('[摘要]') and not _c.startswith('[合并]'):
                                                _c = f'[摘要] {_c}'
                                            update_list.append((idx, _c))
                                        except ValueError:
                                            pass
                        elif line.lower().startswith("cursor="):
                            cursor_str = line.split("=", 1)[1].strip()
                            try:
                                cursor_idx = int(cursor_str)
                            except ValueError:
                                pass

                    if not keep_idxs:
                        raise ValueError("No keep= line found in sub-agent reply")

                    # 计算删除列表：所有 idx - 保留 idx
                    all_force_idxs = set(_f_idx_to_id.keys())
                    delete_idxs = all_force_idxs - keep_idxs

                    # 转换为 UUID
                    deletes = [_f_idx_to_id[i] for i in sorted(delete_idxs) if i in _f_idx_to_id]
                    # 砍半掉的前半段 msg_ids 加入删除列表
                    if _force_halved_msg_ids:
                        deletes.extend(_force_halved_msg_ids)
                    for idx, _ in update_list:
                        if idx not in _f_idx_to_id:
                            logger.warning(f"[Compact] Force LLM returned out-of-range update idx {idx}, silently dropped")
                    updates = [
                        {"message_id": _f_idx_to_id[idx], "content": content}
                        for idx, content in update_list if idx in _f_idx_to_id
                    ]
                    # cursor 转换为 UUID
                    if cursor_idx and cursor_idx in _f_idx_to_id:
                        new_compress_id = _f_idx_to_id[cursor_idx]
                    else:
                        logger.warning(f"[Compact] Force cursor idx {cursor_idx} not in mapping, keeping last_compress_id")
                        # new_compress_id 保持初始值（last_compress_id）

                    logger.info(f"[Tidy] Force: Parsed from content: keep={len(keep_idxs)}, delete={len(deletes)}, update={len(updates)}, cursor_idx={cursor_idx}")

                    # 安全协议：pause ChatQueue + acquire chat_lock
                    from niu_api.chat import _chat_lock
                    _f_chat_lock_acquired = False
                    _fq = None

                    if chat_lock_already_held:
                        _f_chat_lock_acquired = False
                        logger.info("[Tidy] Force: chat_lock already held by caller, skipping ChatQueue pause+lock acquire")
                    else:
                        from niu_api.chat_queue import get_chat_queue
                        _fq = get_chat_queue()
                        _fq.pause()

                        try:
                            await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
                            _f_chat_lock_acquired = True
                        except TimeoutError:
                            logger.warning("[Tidy] Force: chat_lock 60s timeout, aborting execution")

                        if not _f_chat_lock_acquired:
                            _fq.resume()
                            raise RuntimeError("Force: chat_lock timeout")

                        if _fq._processing and _fq._processing_done.is_set():
                            pass
                        elif _fq._processing:
                            try:
                                await asyncio.wait_for(_fq._processing_done.wait(), timeout=30.0)
                            except TimeoutError:
                                logger.warning("[Tidy] Force: ChatQueue processing timeout, aborting execution")
                                if _f_chat_lock_acquired:
                                    _chat_lock.release()
                                _fq.resume()
                                raise RuntimeError("Force: ChatQueue processing timeout") from None

                    try:
                        # 重新获取消息列表
                        fresh_messages = await store.get_messages()
                        existing_ids = {getattr(m, "id", "") for m in fresh_messages}
                        valid_deletes = [mid for mid in deletes if mid in existing_ids]
                        valid_deletes = list(dict.fromkeys(valid_deletes))
                        # 校验游标有效性
                        if new_compress_id and new_compress_id not in existing_ids:
                            logger.warning(f"[Tidy] Force: last_compress_id {new_compress_id} not in messages, reverting to {last_compress_id}")
                            new_compress_id = last_compress_id
                        if new_compress_id and new_compress_id not in existing_ids:
                            logger.warning(f"[Tidy] Force: Fallback last_compress_id {new_compress_id} also invalid, clearing cursor")
                            new_compress_id = ""

                        # 保护游标
                        cursor_ids_set = {cid for cid in [new_compress_id, new_entity_id, new_dream_id] if cid}
                        for cursor_id in cursor_ids_set:
                            if cursor_id in valid_deletes:
                                valid_deletes.remove(cursor_id)
                                logger.warning(f"[Tidy] Force: Protected cursor message {cursor_id} from deletion")
                        valid_updates = [u for u in updates if isinstance(u, dict) and u.get("message_id") and u["message_id"] in existing_ids]
                        cursor_updates = [u for u in valid_updates if u.get("message_id", "") in cursor_ids_set]
                        if cursor_updates:
                            logger.warning(f"[Tidy] Force: Removing cursor messages from updates: {[u.get('message_id') for u in cursor_updates]}")
                            valid_updates = [u for u in valid_updates if u.get("message_id", "") not in cursor_ids_set]
                        # dream 安全边界
                        if new_dream_id:
                            dream_boundary_idx = -1
                            for i, m in enumerate(fresh_messages):
                                if (getattr(m, "id", "") or "") == new_dream_id:
                                    dream_boundary_idx = i
                                    break
                            if dream_boundary_idx >= 0:
                                post_dream_ids = {getattr(m, "id", "") for m in fresh_messages[dream_boundary_idx + 1:]}
                                unsafe_deletes = [mid for mid in valid_deletes if mid in post_dream_ids]
                                if unsafe_deletes:
                                    logger.warning(f"[Tidy] Force: Protecting {len(unsafe_deletes)} messages after dream cursor from deletion")
                                    valid_deletes = [mid for mid in valid_deletes if mid not in post_dream_ids]
                                unsafe_updates = [u for u in valid_updates if u.get("message_id", "") in post_dream_ids]
                                if unsafe_updates:
                                    logger.warning(f"[Tidy] Force: Protecting {len(unsafe_updates)} messages after dream cursor from content replacement")
                                    valid_updates = [u for u in valid_updates if u.get("message_id", "") not in post_dream_ids]
                        # 保护近期消息
                        # 防御 UUID 幻觉：PROTECTED 消息已从输入中排除，但 LLM 可能幻觉出其 UUID
                        protected_force_ids: set[str] = set()
                        if protect_recent_count > 0:
                            _protect_start = _find_protected_range(fresh_messages, protect_recent_count)
                            protected_force_ids = {getattr(fresh_messages[i], "id", "") or "" for i in range(_protect_start, len(fresh_messages))}
                            removed_deletes = [mid for mid in valid_deletes if mid in protected_force_ids]
                            if removed_deletes:
                                logger.warning(f"[Tidy] Force: Protecting {len(removed_deletes)} recent messages from deletion: {removed_deletes}")
                                valid_deletes = [mid for mid in valid_deletes if mid not in protected_force_ids]
                            removed_updates = [u for u in valid_updates if u.get("message_id", "") in protected_force_ids]
                            if removed_updates:
                                logger.warning(f"[Tidy] Force: Protecting {len(removed_updates)} recent messages from update")
                                valid_updates = [u for u in valid_updates if u.get("message_id", "") not in protected_force_ids]
                        # 防止 delete/update 重叠
                        update_ids = {u.get("message_id", "") for u in valid_updates}
                        overlap_ids = update_ids & set(valid_deletes)
                        if overlap_ids:
                            logger.warning(f"[Tidy] Force: Removing {len(overlap_ids)} IDs from deletes that also appear in updates: {overlap_ids}")
                            valid_deletes = [mid for mid in valid_deletes if mid not in overlap_ids]
                        if len(valid_deletes) < len(deletes):
                            logger.warning(f"[Tidy] Force: Filtered {len(deletes) - len(valid_deletes)} invalid delete IDs")
                        if len(valid_updates) < len(updates):
                            logger.warning(f"[Tidy] Force: Filtered {len(updates) - len(valid_updates)} invalid update IDs")

                        # 级联删除
                        _cascade_protected = cursor_ids_set | (protected_force_ids if protect_recent_count > 0 else set())
                        cascade_del = _cascade_tool_chain_deletes(fresh_messages, valid_deletes, protected_ids=_cascade_protected)
                        valid_deletes = cascade_del.delete_ids
                        dangling_tc_cleanups = cascade_del.dangling_cleanups
                        cascade_upd = _cascade_tool_chain_updates(fresh_messages, valid_updates)
                        valid_updates = cascade_upd.updates
                        cascade_delete_ids = cascade_upd.cascade_delete_ids
                        if cascade_delete_ids:
                            existing = set(valid_deletes)
                            for cid in cascade_delete_ids:
                                if cid not in existing:
                                    valid_deletes.append(cid)
                                    existing.add(cid)

                        _post_update_ids = {u.get("message_id", "") for u in valid_updates}
                        _post_overlap = _post_update_ids & set(valid_deletes)
                        if _post_overlap:
                            logger.warning(f"[Tidy] Force: Cascade created delete/update overlap: {_post_overlap}")
                            valid_deletes = [mid for mid in valid_deletes if mid not in _post_overlap]

                        if dangling_tc_cleanups:
                            for cleanup in dangling_tc_cleanups:
                                mid = cleanup["message_id"]
                                dangling_ids = cleanup["dangling_tc_ids"]
                                m = next((m for m in fresh_messages if getattr(m, "id", "") == mid), None)
                                if m and getattr(m, "tool_calls", None):
                                    tcs = m.tool_calls
                                    if isinstance(tcs, str):
                                        tcs = json.loads(tcs)
                                    valid_tcs = [tc for tc in tcs if tc.get("id", "") not in dangling_ids]
                                    if valid_tcs:
                                        await _clean_dangling_tool_calls(store, mid, valid_tcs)
                                    else:
                                        await store.update_message(mid, getattr(m, "content", "") or "", clear_tool_calls=True)
                                    logger.info(f"[Tidy] Force: Cleaned {len(dangling_ids)} dangling tool_calls from protected assistant {mid}")

                        if valid_deletes:
                            del_result = await store.delete_messages_by_ids(valid_deletes)
                            logger.info(f"[Tidy] Force: Deleted {del_result.get('deleted_count', 0)} messages, freed {del_result.get('freed_tokens', 0)} tokens")

                        for upd in valid_updates:
                            mid = upd.get("message_id", "")
                            content = upd.get("content", "")
                            if mid and content:
                                clear_tc = upd.get("clear_tool_calls", False)
                                ok = await store.update_message(message_id=mid, content=content, clear_tool_calls=clear_tc)
                                if ok:
                                    logger.info(f"[Tidy] Force: Updated message {mid}")
                                else:
                                    logger.warning(f"[Tidy] Force: Failed to update message {mid}")

                        logger.info(f"[Tidy] Force: Compression plan executed: {len(valid_deletes)} deletes, {len(valid_updates)} updates")
                        await _cleanup_orphan_tool_messages(store)
                    finally:
                        if _f_chat_lock_acquired:
                            _chat_lock.release()
                        if _fq is not None:
                            _fq.resume()
                except ValueError as e:
                    logger.error(f"[Tidy] Force: Failed to parse compression plan: {e}")
                except Exception as e:
                    logger.error(f"[Tidy] Force: Failed to execute compress plan: {e}")

                # 写入 compress 游标
                if new_compress_id:
                    _write_cursor_with_lock(compress_cursor_path, {
                        "last_compress_id": new_compress_id,
                        "last_compress_at": datetime.now().isoformat(),
                    })
                    logger.info(f"[Tidy] Force: Compress cursor updated: last_compress_id={new_compress_id}")

                # 计算压缩后 token 数（用于降级判断）
                tokens_after = display_tokens  # 默认值
                try:
                    post_messages = await store.get_messages()
                    from agent.token_calculator import TokenCalculator
                    calc = TokenCalculator.get()
                    post_total = 0
                    for pm in post_messages:
                        try:
                            t = calc.count_message_single(pm.role, pm.content or "", tool_calls=getattr(pm, "tool_calls", None))
                        except Exception:
                            t = max(1, len(pm.content or "") // 2) + 4
                        post_total += t
                    tokens_after = post_total
                except Exception:
                    pass

                return {"status": "ok", "mode": "force", "tokens_before": display_tokens, "tokens_after": tokens_after}

            if request.get("skip_compress"):
                logger.info("[Tidy] Force: skip_compress=True, skipping context-manager compression")
                return {"status": "ok", "mode": "force", "skip_compress": True,
                        "tokens_before": display_tokens, "tokens_after": display_tokens}
            return await _compress_force()

        else:
            logger.warning(f"[Tidy] Unknown mode: {mode}, skipping")
            return {"status": "error", "message": f"Unknown mode: {mode}. Use 'sleep' or 'force'."}

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

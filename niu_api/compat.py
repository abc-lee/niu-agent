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
from datetime import datetime

from agent.session import get_message_store
from agent.subagent import _read_context_window_tokens, _read_target_threshold, _read_protect_recent_count, _read_warning_threshold
from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel
from typing import NamedTuple


class CascadeDeleteResult(NamedTuple):
    delete_ids: list[str]
    dangling_cleanups: list[dict]


class CascadeUpdateResult(NamedTuple):
    updates: list[dict]
    cascade_delete_ids: list[str]


def _extract_cursor_id(text: str, field_name: str, valid_ids: set) -> str | None:
    """
    从文本中提取游标 UUID 并验证其存在于消息列表中。

    支持多种 JSON 格式变体：
    - {"field": "uuid"}
    - {"field":"uuid"}（无空格）
    - {"field" : "uuid"}（多空格）
    - 带换行符的 JSON

    Args:
        text: 待搜索的文本（子 Agent 结果或 partial_result）
        field_name: 游标字段名（如 "last_entity_extract_id"）
        valid_ids: 当前消息列表中有效的 UUID 集合

    Returns:
        验证通过的 UUID，"NULL"（明确返回 null），或 None（未找到或无效）
    """
    if not text:
        return None
    # 先检查 null 匹配：区分"没报告"和"明确返回null"
    null_pattern = rf'\{{\s*"{re.escape(field_name)}"\s*:\s*null\s*[,\}}]'
    if re.search(null_pattern, text, re.DOTALL):
        return "NULL"
    # 宽松匹配：允许各种空白格式
    pattern = rf'\{{\s*"{re.escape(field_name)}"\s*:\s*"([^"]+)"\s*'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    candidate = match.group(1)
    if valid_ids is not None and candidate not in valid_ids:
        logger.warning(f"[Tidy] Extracted {field_name}={candidate} not in message list, discarding")
        return None
    return candidate


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


async def _cleanup_orphan_tool_messages(store):
    """清理 DB 中所有孤立的 tool 消息（没有对应 assistant tool_calls 的 tool 输出）。

    压缩可能遗留孤立 tool 消息（旧 bug 或模式2/3 没有完整性验证步骤）。
    这些消息在 agent_loop 加载时会被安全网跳过，但占用 DB 空间。
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
        if getattr(m, "role", "") == "tool":
            tc_call_id = getattr(m, "tool_call_id", "") or ""
            if tc_call_id and tc_call_id not in _valid_tc_ids:
                _orphan_mids.append(getattr(m, "id", ""))
    if _orphan_mids:
        logger.info(f"[Tidy] Cleaning up {len(_orphan_mids)} orphan tool messages from DB")
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


def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None, end_cursor_id: str | None = None, protect_recent: int = 0) -> str:
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
        _protected_positions = set()
        _count = 0
        for rp in range(total_count - 1, -1, -1):
            _, m = range_messages_with_pos[rp]
            if getattr(m, "role", "") in ("user", "assistant"):
                _protected_positions.add(rp)
                _count += 1
                if _count >= protect_recent:
                    break
    for rel_pos, (orig_pos, msg) in enumerate(range_messages_with_pos):
        original_idx = start + orig_pos + 1  # 1-based display index（使用原始位置）
        msg_id = getattr(msg, "id", "") or ""
        out_msg_ids.append(msg_id)
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + orig_pos) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + orig_pos]}tokens "
        # protect_recent: 对最后 N 条 user/assistant 消息加 [PROTECTED] 标签（不保护 role=tool 的工具输出）
        protected_label = ""
        if protect_recent > 0 and _protected_positions is not None and rel_pos in _protected_positions:
            protected_label = "[PROTECTED] "
        lines.append(f"[id:{msg_id}] [idx:{original_idx}] {token_annotation}{msg.role}: {protected_label}{content}")

    if not lines:
        return "（无新增消息）"

    return f"共 {len(lines)} 条新消息\n\n" + "\n".join(lines)


def _estimate_text_tokens(text: str) -> int:
    """粗略估算文本 token 数（中文约1.5字/token，英文约4字/token，取中间值2字/token）"""
    return len(text) // 2


def _truncate_preserving_tail(text: str, max_tokens: int) -> str:
    """截断文本，保留末尾近端消息（远端从开头截断）。
    消息列表在 prompt 末尾，开头是远端(idx小的)，末尾是近端(idx大的)。
    截断远端保留近端，确保 LLM 能看到需要保护的消息。"""
    max_chars = max_tokens * 2  # 反向估算字符数
    if len(text) <= max_chars:
        return text
    # 保留末尾近端部分，截断开头远端
    kept_tail = text[-max_chars:]
    # 找到第一个完整的消息行（以 [id: 开头）
    first_line_pos = kept_tail.find("[id:")
    if first_line_pos > 0:
        kept_tail = kept_tail[first_line_pos:]
    # 更新消息计数
    line_count = kept_tail.count("[id:")
    return f"共约 {line_count} 条消息（远端部分已省略。当前可见消息均属于中端区和近端区，按相对位置划分区域即可）\n\n" + kept_tail


def _truncate_preserving_both(text: str, max_tokens: int) -> str:
    """双向截断：保留开头指令 + 末尾近端消息，截断中间远端消息。
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
        # 消息列表部分：保留末尾近端消息
        tail_budget = max_chars - len(head) - 200
        if tail_budget > 0 and len(msg_text) > tail_budget:
            tail = msg_text[-tail_budget:]
            first_msg = tail.find("[id:")
            if first_msg > 0:
                tail = tail[first_msg:]
            msg_count = tail.count("[id:")
            return head + f"[远端消息已省略，保留近端 {msg_count} 条消息。可见消息从远端区中后段开始，按相对位置划分区域]\n\n" + tail
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
    return head + "\n\n[中间远端消息已省略，保留近端 " + str(msg_count) + " 条消息。可见消息从远端区中后段开始，按相对位置划分区域]\n\n" + tail


def _build_journal_task(journal_msg_text: str, safe_tokens: int = 0) -> str:
    """构建 journal-agent 的 task prompt（增量消息嵌入）。

    Args:
        journal_msg_text: _build_incremental_msg_text() 返回的增量消息文本
        safe_tokens: 截断 token 上限（0 表示不截断）

    Returns:
        完整的 task prompt 字符串
    """
    prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

    if safe_tokens > 0:
        prompt = _truncate_task_for_subagent(prompt, safe_tokens)
    return prompt


def _write_cursor_with_lock(cursor_path, data: dict) -> None:
    """带文件锁保护的游标写入 — 防止 handler/compat/runner 并发竞争。"""
    import fcntl
    lock_path = cursor_path.with_suffix(".lock")
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            cursor_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)



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


def _should_auto_tidy(current_tokens: int, context_window_tokens: int = 0) -> bool:
    """已禁用：压缩只在 agent_loop 工具循环中同步触发，不在对话后异步触发。"""
    return False


async def _check_and_trigger_auto_tidy(store):
    # DEPRECATED: no callers — compress only triggers in agent_loop tool loop
    """
    检查是否需要自动增量整理，如需要则异步触发。

    在每次 assistant 消息写入后调用。
    整理以 sleep 模式异步执行，不阻塞 chat 响应。
    不做 locked() 前置检查 — _run_auto_tidy 内部的锁机制处理重入保护。
    """
    try:
        messages = await store.get_messages()
        if not messages:
            return

        current_tokens = _estimate_total_tokens(messages)
        context_window_tokens = _read_context_window_tokens()

        if not _should_auto_tidy(current_tokens, context_window_tokens=context_window_tokens):
            return

        usage_pct = f"{current_tokens/context_window_tokens:.1%}" if context_window_tokens > 0 else "N/A"
        logger.info(f"[AutoTidy] Triggering sleep tidy: tokens={current_tokens}, usage={usage_pct}")

        # 异步触发 sleep 模式整理（_run_auto_tidy 内部有 _tidy_lock 防重入）
        asyncio.create_task(_run_auto_tidy())
    except Exception as e:
        logger.warning(f"[AutoTidy] Check failed: {e}")


_tidy_lock = asyncio.Lock()


async def _run_auto_tidy():
    # DEPRECATED: no callers — compress only triggers in agent_loop tool loop
    """自动整理：非阻塞获取锁，避免与手动触发竞争或无限阻塞。"""
    try:
        # 非阻塞获取锁：如果锁已被占用（force tidy 或手动触发），直接跳过
        try:
            await asyncio.wait_for(_tidy_lock.acquire(), timeout=0.01)
        except TimeoutError:
            logger.info("[AutoTidy] Tidy already running, skipping")
            return

        try:
            result = await _tidy_context_impl(request={"session_id": "default", "mode": "sleep"})
            if result.get("status") == "error":
                logger.warning(f"[AutoTidy] tidy_context returned error: {result}")
            else:
                logger.info(f"[AutoTidy] Completed successfully")
        finally:
            _tidy_lock.release()
    except Exception as e:
        logger.warning(f"[AutoTidy] Failed: {e}")


router = APIRouter(tags=["compat"])

# 并发锁：串行化所有 chat 请求，防止并发调用 runner.chat() 导致共享状态损坏
_chat_lock = asyncio.Lock()


class ChatRequest(BaseModel):
    """Chat request"""

    message: str
    session_id: str | None = None
    resources: list = []


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


def set_preload_complete():
    """Mark preload as complete"""
    global _preload_complete
    _preload_complete = True
    logger.info("Preload marked as complete")


@router.get("/api/llm-status")
async def get_llm_status() -> dict:
    """检测 LLM 是否已配置可用（直接从文件读取，不走缓存）"""
    import json
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})
        api_key = llm.get("apiKey", "")
        api_base = llm.get("apiBase", "")
        model = llm.get("model", "")

        if not api_key:
            return {"ready": False, "error": "API key not configured"}
        if not api_base or not model:
            return {"ready": False, "error": "API base or model not configured"}
        return {"ready": True}
    except Exception as e:
        return {"ready": False, "error": str(e)}


@router.post("/api/test-llm")
async def test_llm(request: Request) -> dict:
    """通过真实 LLM 调用验证配置。验证完整链路：config → LiteLLM → provider 路由 → API 调用 → 响应。

    请求体可选：传入 config 字典则用它测试（配置页面预保存测试）；
    不传或为空则从 user-config.json 读取（启动器验证）。
    """
    from niu_api.llm_proxy import get_llm_config
    from agent.generic.litellm_adapter import LiteLLMSession

    # 读取配置：优先用请求体，否则从文件读取
    try:
        body = await request.json()
    except Exception:
        body = {}

    # 统一键名为小写（前端传 apiKey/apiBase，get_llm_config 返回小写，需统一）
    body = {k.lower(): v for k, v in body.items()} if body else {}

    if body and body.get("apikey"):
        # 配置页面传入的表单值（预保存测试）
        config = body
    else:
        # 启动器调用：从文件读取
        try:
            config = get_llm_config()
        except Exception as e:
            return {"success": False, "error": f"读取配置失败: {e}"}

    # body 已归一化，config 也需要确保小写
    config = {k.lower(): v for k, v in config.items()}

    if not config.get("apikey"):
        return {"success": False, "error": "API Key 未配置"}
    if not config.get("apibase"):
        return {"success": False, "error": "API 地址未配置"}
    if not config.get("model"):
        return {"success": False, "error": "模型名称未配置"}

    try:
        llm_config = {
            "api_type": config.get("type", "openai"),
            "apikey": config["apikey"],
            "apibase": config["apibase"],
            "model": config["model"],
            "reasoning_effort": None,
            "provider": config.get("provider", ""),
            "litellm_kwargs": {**config.get("litellm_kwargs", {}), "max_tokens": 5},
            "read_timeout": 10,
        }
        session = LiteLLMSession(cfg=llm_config)

        def _sync_test():
            gen = session.chat(messages=[{"role": "user", "content": "hi"}])
            chunks = []
            mock_resp = None
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                mock_resp = e.value
            # 思考模型可能只输出 reasoning_content 而无文本 chunk，
            # 但 MockResponse 会包含 thinking/content 字段
            text = "".join(chunks)
            has_content = bool(text.strip()) or (
                mock_resp is not None and (getattr(mock_resp, 'content', None) or getattr(mock_resp, 'thinking', None))
            )
            return text, has_content

        result, has_content = await asyncio.wait_for(asyncio.to_thread(_sync_test), timeout=20)
        if not has_content:
            return {"success": False, "error": "模型返回空响应"}

        provider = config.get("provider", "") or config.get("type", "openai")
        return {"success": True, "message": f"模型测试通过 (model={config.get('model')}, provider={provider})"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "连接超时，请检查网络和 API 地址"}
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "unauthorized" in error_msg.lower() or "invalid api key" in error_msg.lower():
            return {"success": False, "error": "API Key 无效或未授权"}
        if "404" in error_msg or "not found" in error_msg.lower():
            return {"success": False, "error": "模型或 API 端点不存在，请检查模型名称和地址"}
        # Sanitize error message to avoid leaking API keys in URLs/headers
        import re
        safe_msg = re.sub(r'key=[^&\s]+', 'key=***', error_msg)
        safe_msg = re.sub(r'Bearer\s+[^\s]+', 'Bearer ***', safe_msg)[:200]
        if "provider" in error_msg.lower() or "unmapped" in error_msg.lower():
            return {"success": False, "error": f"Provider 路由错误: {safe_msg}"}
        return {"success": False, "error": f"模型测试失败: {safe_msg}"}


@router.get("/api/preload-status")
async def get_preload_status():
    """Get preload status - used by Go launcher to wait before showing window"""
    return {"ready": _preload_complete, "uptime": str(datetime.now() - _startup_time).split(".")[0]}


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
async def get_stats() -> StatsResponse:
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
        try:
            from niu_api.chat import get_or_create_runner
            runner = get_or_create_runner()
            real_tokens = getattr(getattr(runner, 'handler', None), '_last_prompt_tokens', 0) or 0
        except Exception:
            pass
        if real_tokens > 0:
            context_usage = real_tokens / context_window if context_window > 0 else 0.0
        else:
            all_msgs = await store.get_messages()
            total_tokens = _estimate_total_tokens(all_msgs)
            context_usage = total_tokens / context_window if context_window > 0 else 0.0
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

        # Run chat using asyncio.to_thread to avoid blocking event loop
        def sync_chat():
            chunks = []
            for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner, resources=request.resources or None):
                chunks.append(chunk)
            return "".join(chunks)

        try:
            full_reply = await asyncio.to_thread(sync_chat)
        except Exception as e:
            import traceback
            logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
            full_reply = f"Error: {str(e)}"

        # 双管道持久化：使用 persist_agent_reply 统一处理
        rv = getattr(runner, "last_return_value", None)
        from niu_api.chat import persist_agent_reply
        persisted_msgs = getattr(runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
        message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs)

        # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞）
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat Session] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"triggering force compression (blocking)"
            )
            try:
                await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[Chat Session] Force compression skipped: tidy lock held by another operation")
                tidy_result = {"status": "skipped", "reason": "lock_busy"}
            else:
                try:
                    tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"}, chat_lock_already_held=True)
                finally:
                    _tidy_lock.release()
                logger.info(f"[Chat Session] Force compression result: {tidy_result.get('status')}")
        return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)
    finally:
        from agent.runner import clear_stop, drain_supplements
        clear_stop()  # 防御性清除：确保停止标志不残留
        drain_supplements()  # 清理残留补充消息，防止被 ChatQueue 路径读取
        _chat_lock.release()


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


@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    """Clear all messages (for /new and /clear commands)"""
    # 先请求停止当前 Agent 工作
    from agent.runner import request_stop, clear_stop
    request_stop()

    # 获取锁，防止与正在进行的 chat 冲突
    # 超时增加到 30 秒，等待 Agent 循环检测 stop 标志并退出
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=30.0)
    except TimeoutError:
        logger.warning("[clear_chat] _chat_lock 30s timeout, clear rejected")
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

            # Note: LLM session history is managed by ContextManager,
            # which reloads from message store each call.
            # store.clear_messages() above already clears persistent history.

        # 清空临时目录（画框图片等）
        from agent.tmp_dir import cleanup_all_tmp
        cleaned_tmp = cleanup_all_tmp()

        # 重置游标文件（消息已清空，旧游标指向不存在的消息）
        from pathlib import Path
        for cursor_name in ["last_entity_extract.json", "last_dream_evolve.json", "last_compress.json", "last_journal.json"]:
            cursor_p = Path.home() / ".niu" / cursor_name
            try:
                if cursor_p.exists():
                    cursor_p.unlink()
            except OSError as e:
                logger.warning(f"[clear_chat] Failed to reset cursor file {cursor_name}: {e}")

        return {"success": True, "deleted_count": count, "cleaned_tmp": cleaned_tmp}
    finally:
        _chat_lock.release()


@router.get("/api/pending-alerts")
async def get_pending_alerts() -> list:
    """Get pending alerts - delegates to alerts module"""
    from niu_api.alerts import get_and_clear_pending_alerts
    return get_and_clear_pending_alerts()


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
    # 加锁防止并发：手动触发和自动触发互斥，超时10秒避免死锁
    try:
        await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
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
    session_id = request.get("session_id", "default")
    mode = request.get("mode", "sleep")

    logger.info(f"[Tidy] Context tidy triggered: session={session_id}, mode={mode}")

    try:
        # Get message store
        store = await get_message_store()
        messages = await store.get_messages()

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

        from agent.subagent import call_subagent
        from agent.runner import is_stop_requested, clear_stop

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

        msg_id_set = {getattr(m, "id", "") or "" for m in messages}  # 用于游标 ID 有效性校验

        if mode == "sleep":
            # Sleep mode: entity-extractor (增量) → dream-evolver (增量) → context-manager (增量)

            # 1/3. entity-extractor（增量，task 方式）
            entity_msg_ids = []
            entity_msg_text = _build_incremental_msg_text(
                messages, last_entity_extract_id, entity_msg_ids, msg_tokens
            )
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_prompt_prefix = """以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

"""
            entity_prompt_suffix = """

处理完成后，在报告末尾用 JSON 格式报告：{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}
**必须推进游标**：即使没有可提取的内容（全是程序化操作、闲聊等），也必须输出 idx 最大的消息的 UUID。只有当传入的消息列表本身为空（一条消息都没有）时，才输出 {"last_entity_extract_id": null}"""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                entity_full_prompt = entity_prompt_prefix + entity_msg_text + entity_prompt_suffix

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_entity_prompt = _truncate_task_for_subagent(entity_full_prompt, safe_tokens)

                def run_entity_extractor():
                    return call_subagent(
                        agent_name="entity-extractor",
                        task=truncated_entity_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=None,
                    )

                entity_result = await asyncio.to_thread(run_entity_extractor)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] entity-extractor result: {entity_result[:200]}")

                # 游标提取和推进
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_entity_id = recovered
                        logger.info(f"[Tidy] Entity cursor recovered from partial_result: {new_entity_id}")
                    else:
                        new_entity_id = entity_msg_ids[-1]
                        logger.warning(f"[Tidy] Entity cursor overflow fallback to last incremental msg: {new_entity_id}")
                else:
                    extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_entity_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_entity_id = entity_msg_ids[-1]
                        logger.warning(f"[Tidy] Entity cursor not matched or null, advancing to last incremental msg: {new_entity_id}")
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
            msg_id_set = {getattr(m, "id", "") for m in messages}
            dream_msg_ids = []
            dream_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_dream_prompt = _truncate_task_for_subagent(dream_prompt, safe_tokens)

                def run_dream_evolver():
                    return call_subagent(
                        agent_name="dream-evolver",
                        task=truncated_dream_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_dream_evolve_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_dream_id = recovered
                        logger.info(f"[Tidy] Dream cursor recovered from partial_result: {new_dream_id}")
                    else:
                        new_dream_id = dream_msg_ids[-1]
                        logger.warning(f"[Tidy] Dream cursor overflow fallback to last incremental msg: {new_dream_id}")
                else:
                    extracted = _extract_cursor_id(dream_result, "last_dream_evolve_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_dream_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_dream_id = dream_msg_ids[-1]
                        logger.warning(f"[Tidy] Dream cursor not matched or null, advancing to last incremental msg: {new_dream_id}")
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
                msg_id_set = {getattr(m, "id", "") for m in messages}

                new_journal_id = last_journal_id
                journal_msg_ids = []
                journal_msg_text = _build_incremental_msg_text(
                    messages, last_journal_id, journal_msg_ids, msg_tokens
                )
                logger.info(f"[Tidy] Sleep: starting journal-agent ({len(journal_msg_ids)} incremental messages)")

                if journal_msg_ids:
                    context_window_for_truncate = _read_context_window_tokens()
                    safe_tokens = int(context_window_for_truncate * 0.6)
                    truncated_journal_prompt = _build_journal_task(journal_msg_text, safe_tokens)

                    def run_journal_agent():
                        return call_subagent(
                            agent_name="journal-agent",
                            task=truncated_journal_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                        )

                    journal_result = await asyncio.to_thread(run_journal_agent)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] journal-agent result: {journal_result[:200]}")

                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        partial = overflow_info.get("partial_result", "")
                        recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                        if recovered and recovered != "NULL":
                            new_journal_id = recovered
                        else:
                            new_journal_id = journal_msg_ids[-1]
                            logger.warning(f"[Tidy] Journal cursor overflow fallback: {new_journal_id}")
                    else:
                        extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                        if extracted and extracted != "NULL":
                            new_journal_id = extracted
                        elif extracted == "NULL" or not extracted:
                            new_journal_id = journal_msg_ids[-1]
                            logger.warning(f"[Tidy] Journal cursor not matched, fallback: {new_journal_id}")

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
            msg_id_set = {getattr(m, "id", "") for m in messages}
            compress_msg_ids = []
            # 读取保护数量配置
            protect_recent_count = _read_protect_recent_count()

            # 模式二：始终全量传入（无游标机制），模式一：增量范围
            _is_mode2 = usage_percent >= 50
            _compress_cursor = "" if _is_mode2 else last_compress_id
            _end_cursor = None if _is_mode2 else new_dream_id
            compress_msg_text = _build_incremental_msg_text(
                messages, _compress_cursor, compress_msg_ids, msg_tokens,
                end_cursor_id=_end_cursor, protect_recent=protect_recent_count
            )

            if not _is_mode2:
                # 模式一：限制增量范围的 token 总量，避免截断砍掉近端消息
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
            # 模式二量化目标：基于 targetThreshold 计算动态目标（提前计算，决定是否跳过）
            _compress_target = ""
            _cursor_instruction = ""
            if _skip_compress:
                pass  # 接近强制阈值，跳过所有压缩
            elif _is_mode2:
                target_threshold = _read_target_threshold()
                target_tokens = int(context_window_tokens * target_threshold)
                suggest_release = max(display_tokens - target_tokens, 0)
                if suggest_release == 0:
                    # 当前已在目标范围内，不需要压缩
                    logger.info(f"[Tidy] Mode-2: already at target, skipping compression")
                    _skip_compress = True
                elif suggest_release < int(display_tokens * 0.05):
                    # 释放量太小（<5%），不值得压缩一轮，跳过
                    logger.info(f"[Tidy] Mode-2: suggest_release {suggest_release} < 5%, skipping compression")
                    _skip_compress = True
                elif suggest_release > 0:
                    _compress_target = (
                        f"\n压缩目标：\n"
                        f"- 目标 token 总数：{target_tokens}（{target_threshold*100:.0f}%）\n"
                        f"- 需释放至少 {suggest_release} tokens\n"
                        f"优先压缩远端（idx 小的）消息；"
                        f"如果远端+中端释放量不足目标，可对近端非保护消息按中端区规则（合并为摘要）处理，但不突破 [PROTECTED] 边界；"
                        f"如果近端非保护消息也全部处理后仍不足目标，接受当前结果。\n"
                    )
                # 模式二改为一轮JSON方案，不要求游标报告
                _cursor_instruction = ""
            else:
                # 模式一需要报告游标
                _cursor_instruction = """处理完成后，在报告末尾用 JSON 格式报告：{"last_compress_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}
**必须推进游标**：即使没有需要处理的内容，也必须输出 idx 最大的消息的 UUID。"""
            logger.info(f"[Tidy] Sleep: usage={usage_percent:.1f}%, selecting {compress_mode}")

            new_compress_id = last_compress_id
            if compress_msg_ids and not _skip_compress:
                # 构建保护消息 UUID 列表（只含 user/assistant 消息，不含 tool 输出）
                # 直接从完整 messages 列表计算，不依赖截断后的 compress_msg_ids
                # 这样即使截断移除了近端消息，受保护消息的 ID 仍然完整
                _pids = []
                for i in range(len(messages) - 1, -1, -1):
                    _m = messages[i]
                    if getattr(_m, "role", "") in ("user", "assistant"):
                        _pids.insert(0, getattr(_m, "id", "") or "")
                    if len(_pids) >= protect_recent_count:
                        break
                protected_ids = _pids  # No fallback: tool output is never protected

                if _is_mode2:
                    compress_plan_path = os.path.expanduser("~/.niu/compress_plan_mode2.json")
                    if os.path.exists(compress_plan_path):
                        try:
                            os.remove(compress_plan_path)
                        except OSError:
                            pass

                    prompt = f"""压缩上下文：当前{display_tokens} tokens（{usage_percent:.1f}%），需释放至{target_tokens} tokens以下。

消息列表（每条带[idx:N]序号）：
{compress_msg_text}

[PROTECTED]标记的消息不可动。直接回复两行文本，不要调用任何工具，不要输出其他任何内容：
第1行：keep=保留的idx序号，逗号分隔，支持范围如1-5
第2行：update=需压缩的idx序号|摘要内容，多个用分号分隔
示例：
keep=1,2,5-10,15
update=3|用户讨论了XX方案;11|工具执行了YY操作

压缩规则（必须遵守）：
- 按事务合并：属于同一件事的多轮交互（用户要求→工具调用→结果），合并为一条摘要
- 远端摘要格式："用户要求X，最终Y"（只保留意图和结果，丢弃过程）
- 近端摘要格式："用户要求X，调用Z工具，得到Y"（保留关键工具和输出）
- role=tool 的工具输出：不需要放入keep，会被程序自动删除
- 纯确认回复（"好的""明白了""谢谢"）：不需要放入keep
- 不在keep中的消息会被程序自动删除，所以有价值的对话必须放进keep或update
- update的idx必须在keep中
- 只输出这两行"""
                else:
                    prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并），在单元内应排除不参与压缩：
保护消息ID: {json.dumps(protected_ids)}

消息列表：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。{_cursor_instruction}"""

                # 截断 task 防止子Agent超限 + 子Agent调用 + 结果处理
                if _is_mode2:
                    # === 模式二：一轮write JSON方案 + 程序化安全执行 ===
                    # 模式二只做一轮交互（prompt → 回复 JSON → 结束），不会有第二轮
                    # 所以 prompt 不可能超上下文窗口，不需要截断
                    # 且模式二的核心任务是压缩远端消息，截断远端会导致无法完成压缩
                    # 因此：不做截断，传全量消息给 LLM

                    def run_context_manager_mode2():
                        return call_subagent(
                            agent_name="context-manager",
                            task=prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
                        )

                    compress_result = await asyncio.to_thread(run_context_manager_mode2)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] Mode-2: context-manager completed, length={len(compress_result)}")

                    # 从 LLM content 解析序号格式压缩方案
                    _idx_to_id: dict[int, str] = {}
                    for _i, _mid in enumerate(compress_msg_ids):
                        _idx_to_id[_i + 1] = _mid

                    keep_idxs: set[int] = set()
                    update_list: list[tuple[int, str]] = []
                    for line in compress_result.split('\n'):
                        line = line.strip()
                        if line.lower().startswith('keep='):
                            keep_idxs = _parse_idx_list(line.split('=', 1)[1].strip())
                        elif line.lower().startswith('update='):
                            update_str = line.split('=', 1)[1].strip()
                            for item in update_str.split(';'):
                                if '|' in item:
                                    idx_str, content = item.split('|', 1)
                                    try:
                                        update_list.append((int(idx_str.strip()), content.strip()))
                                    except ValueError:
                                        pass

                    if not keep_idxs:
                        logger.error("[Tidy] Mode-2: No keep= line found in LLM response, compression skipped")
                    else:
                        all_idxs = set(_idx_to_id.keys())
                        delete_idxs = all_idxs - keep_idxs
                        deletes = [_idx_to_id[i] for i in sorted(delete_idxs) if i in _idx_to_id]
                        updates = [
                            {"message_id": _idx_to_id[idx], "content": content}
                            for idx, content in update_list if idx in _idx_to_id
                        ]
                        for idx, content in update_list:
                            if idx not in keep_idxs and idx in _idx_to_id:
                                logger.warning(f"[Tidy] Mode-2: update idx {idx} not in keep set")
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
                        except asyncio.TimeoutError:
                            logger.warning("[Tidy] Mode-2: chat_lock 60s timeout, aborting execution")

                        if not _chat_lock_acquired:
                            _q.resume()
                            raise RuntimeError("chat_lock timeout")

                        if _q._processing and _q._processing_done.is_set():
                            pass
                        elif _q._processing:
                            try:
                                await asyncio.wait_for(_q._processing_done.wait(), timeout=30.0)
                            except asyncio.TimeoutError:
                                logger.warning("[Tidy] Mode-2: ChatQueue processing timeout, aborting execution")
                                if _chat_lock_acquired:
                                    _chat_lock.release()
                                _q.resume()
                                raise RuntimeError("ChatQueue processing timeout")

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
                            protected_set: set[str] = set()
                            if protect_recent_count > 0:
                                _pids = []
                                for m in reversed(fresh_messages):
                                    if getattr(m, "role", "") in ("user", "assistant"):
                                        _pids.append(getattr(m, "id", ""))
                                    if len(_pids) >= protect_recent_count:
                                        break
                                protected_set = set(_pids)
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
                                        tcs = getattr(m, "tool_calls")
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
                        return call_subagent(
                            agent_name="context-manager",
                            task=truncated_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
                        )

                    cm_result = await asyncio.to_thread(run_context_manager)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] context-manager result: {cm_result[:200]}")

                    # 游标提取
                    if _is_subagent_overflow(cm_result):
                        overflow_info = _extract_overflow_info(cm_result)
                        logger.warning(f"[Tidy] context-manager overflow: {overflow_info.get('turns_completed', 0)} turns")
                        partial = overflow_info.get("partial_result", "")
                        recovered = _extract_cursor_id(partial, "last_compress_id", msg_id_set)
                        if recovered and recovered != "NULL":
                            new_compress_id = recovered
                        else:
                            new_compress_id = compress_msg_ids[-1]
                            logger.warning(f"[Tidy] Compress cursor overflow fallback: {new_compress_id}")
                    else:
                        extracted = _extract_cursor_id(cm_result, "last_compress_id", msg_id_set)
                        if extracted and extracted != "NULL":
                            new_compress_id = extracted
                        elif extracted == "NULL" or not extracted:
                            new_compress_id = compress_msg_ids[-1]
                            logger.warning(f"[Tidy] Compress cursor not matched, fallback: {new_compress_id}")

                    # 校验游标
                    if new_compress_id:
                        fresh_msgs = await store.get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_compress_id not in fresh_ids:
                            logger.warning(f"[Tidy] Compress cursor {new_compress_id} deleted, reverting to {last_compress_id}")
                            new_compress_id = last_compress_id
                            if new_compress_id and new_compress_id not in fresh_ids:
                                new_compress_id = ""

                    compress_integrity_ok = True
                    if protected_ids:
                        try:
                            protected_originals = {}
                            for pid in protected_ids:
                                _m = next((m for m in messages if getattr(m, "id", "") == pid), None)
                                if _m:
                                    protected_originals[pid] = getattr(_m, "content", "") or ""

                            post_msgs = await store.get_messages()
                            post_ids = {getattr(m, "id", "") for m in post_msgs}
                            post_content_map = {getattr(m, "id", ""): (getattr(m, "content", "") or "") for m in post_msgs}

                            for pid in protected_ids:
                                if pid not in post_ids:
                                    logger.error(f"[Tidy] PROTECTED message {pid} was deleted by context-manager! Cannot restore (add_message would disorder sequence). Blocking cursor advance.")
                                    compress_integrity_ok = False
                                elif pid in protected_originals and pid in post_content_map:
                                    original = protected_originals[pid]
                                    current = post_content_map[pid]
                                    if original != current:
                                        logger.warning(f"[Tidy] PROTECTED message {pid} was modified by context-manager! Rolling back content...")
                                        await store.update_message(pid, original)
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

            # 1/3. entity-extractor（全量 task 方式，cursor 传空 = 全量）
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_force_msg_ids = []
            entity_force_msg_text = _build_incremental_msg_text(
                messages, "", entity_force_msg_ids, msg_tokens
            )
            entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的内容，也必须输出 idx 最大的消息的 UUID。"""

            # 截断 task 防止子Agent超限
            context_window_for_truncate = _read_context_window_tokens()
            safe_tokens = int(context_window_for_truncate * 0.6)
            truncated_entity_force_prompt = _truncate_task_for_subagent(entity_force_prompt, safe_tokens)

            def run_entity_extractor_force():
                return call_subagent(
                    agent_name="entity-extractor",
                    task=truncated_entity_force_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    history=None,
                )

            if entity_force_msg_ids:
                entity_result = await asyncio.to_thread(run_entity_extractor_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: entity-extractor completed, length={len(entity_result)}")

                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_entity_id = recovered
                        logger.info(f"[Tidy] Force: Entity cursor recovered from partial_result: {new_entity_id}")
                    else:
                        new_entity_id = entity_force_msg_ids[-1] if entity_force_msg_ids else last_entity_extract_id
                        logger.warning(f"[Tidy] Force: Entity cursor overflow fallback: {new_entity_id}")
                else:
                    extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_entity_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_entity_id = entity_force_msg_ids[-1] if entity_force_msg_ids else last_entity_extract_id
                        logger.warning(f"[Tidy] Force: Entity cursor not matched, fallback to last msg: {new_entity_id}")
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
            msg_id_set = {getattr(m, "id", "") for m in messages}
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            dream_force_msg_ids = []
            dream_force_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_dream_force_prompt = _truncate_task_for_subagent(dream_force_prompt, safe_tokens)

                def run_dream_evolver_force():
                    return call_subagent(
                        agent_name="dream-evolver",
                        task=truncated_dream_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")

                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_dream_evolve_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_dream_id = recovered
                        logger.info(f"[Tidy] Force: Dream cursor recovered from partial_result: {new_dream_id}")
                    else:
                        new_dream_id = dream_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Dream cursor overflow fallback: {new_dream_id}")
                else:
                    extracted = _extract_cursor_id(dream_result, "last_dream_evolve_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_dream_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_dream_id = dream_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Dream cursor not matched, fallback to last msg: {new_dream_id}")
            else:
                logger.info("[Tidy] Force: dream-evolver no incremental messages")

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
            msg_id_set = {getattr(m, "id", "") for m in messages}

            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            journal_force_msg_text = _build_incremental_msg_text(
                messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_journal_force_prompt = _build_journal_task(journal_force_msg_text, safe_tokens)

                def run_journal_agent_force():
                    return call_subagent(
                        agent_name="journal-agent",
                        task=truncated_journal_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                journal_result = await asyncio.to_thread(run_journal_agent_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: journal-agent completed, length={len(journal_result)}")

                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Tidy] Force: journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_journal_id = recovered
                    else:
                        new_journal_id = journal_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Journal cursor overflow fallback: {new_journal_id}")
                else:
                    extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_journal_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_journal_id = journal_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Journal cursor not matched, fallback: {new_journal_id}")

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

            # 3/3. context-manager force prompt — 一轮 JSON 文件方案
            # 重新读取 compress 游标
            last_compress_id = ""
            if compress_cursor_path.exists():
                try:
                    cdata = json.loads(compress_cursor_path.read_text(encoding="utf-8"))
                    last_compress_id = cdata.get("last_compress_id", "")
                except Exception as e:
                    logger.warning(f"[Tidy] Failed to read compress cursor in force mode: {e}")

            target_tokens = int(context_window_tokens * _read_target_threshold())
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

            # 使用统一的 _build_incremental_msg_text 构建（与模式二一致）
            # 传入 protect_recent 参数，自动标注 [PROTECTED]
            _force_msg_ids = []
            msg_list_text = _build_incremental_msg_text(
                messages, "", _force_msg_ids, msg_tokens,
                end_cursor_id=None, protect_recent=protect_recent_count
            )
            msg_list_text = msg_list_text.replace("条新消息", "条消息", 1)
            msg_id_set = set(_force_msg_ids)

            # 计算 force 路径的受保护 ID
            _f_pids = []
            for i in range(len(messages) - 1, -1, -1):
                _m = messages[i]
                if getattr(_m, "role", "") in ("user", "assistant"):
                    _f_pids.insert(0, getattr(_m, "id", "") or "")
                if len(_f_pids) >= protect_recent_count:
                    break
            protected_force_ids = _f_pids

            # 构建 idx→UUID 映射 + id→idx 反向映射（用于 prompt 和解析）
            _f_idx_to_id: dict[int, str] = {}
            _f_id_to_idx: dict[str, int] = {}
            for _i, _mid in enumerate(_force_msg_ids):
                _f_idx_to_id[_i + 1] = _mid
                _f_id_to_idx[_mid] = _i + 1

            # 计算受保护消息的 idx 列表（用于 prompt 中显示）
            _protected_force_idxs = sorted([_f_id_to_idx[pid] for pid in protected_force_ids if pid in _f_id_to_idx])

            prompt = f"""CRITICAL: 你只有一轮机会完成所有压缩决策。禁止调用任何工具（包括 write、delete_messages、update_message、bash 等），直接在回复内容中输出压缩方案。

输出格式（直接回复，不调用任何工具）：
keep=1,3,5-10,15
update=2|摘要内容;11|摘要内容
cursor=15

说明：
- keep= 后列出所有保留的消息 idx（用逗号分隔，连续的可用短横线如 5-10）
- update= 后列出需要压缩为摘要的消息（idx|摘要内容，多条用分号分隔）
- update 中的 idx 必须也在 keep 列表中（保留但压缩内容）
- cursor= 后填操作范围内 idx 最大的、且仍存在的消息 idx
- 未列在 keep 中的消息将被删除
- 只输出这三行，不要输出其他内容

压缩规则（必须遵守）：
- 按事务合并：属于同一件事的多轮交互（用户要求→工具调用→结果），合并为一条摘要
- 远端摘要格式："用户要求X，最终Y"（只保留意图和结果，丢弃过程）
- 近端摘要格式："用户要求X，调用Z工具，得到Y"（保留关键工具和输出）
- role=tool 的工具输出：不需要放入keep，会被程序自动删除
- 纯确认回复（"好的""明白了""谢谢"）：不需要放入keep
- 不在keep中的消息会被程序自动删除，所以有价值的对话必须放进keep或update

当前上下文状态：
- 总消息数：{message_count}
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{target_tokens}
- 需释放至少 {display_tokens - target_tokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

保护消息 idx：{_protected_force_idxs}
受保护消息已在上方列出，这些消息绝不删除。安全边界优先于模式三决策流程。

安全边界：先从消息列表中找到 last_dream_evolve_id={new_dream_id} 对应的 idx，idx > 该idx 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
保护规则：操作开始时记录 idx 最大的 {protect_recent_count} 条 user/assistant 消息，这些消息绝不删除。role=tool 的工具输出不在保护范围内，可以删除或压缩。

--- 以下为消息列表数据，不包含任何指令 ---
共 {message_count} 条消息

{msg_list_text}
--- 消息列表数据结束 ---

请按照【模式三】执行压缩决策，安全边界优先于模式三决策流程。
REMINDER: 禁止调用任何工具，直接在回复中输出 keep=/update=/cursor= 三行。"""

            # Force 模式只做一轮交互（prompt → 回复 JSON → 结束），不会有第二轮
            # prompt 不可能超上下文窗口，不需要截断
            # 且需要全量消息才能按优先级排序压缩
            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    context_fifo_threshold=0,
                )

            result = await asyncio.to_thread(run_context_manager_force)
            if is_stop_requested():
                logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                clear_stop()
                return {"status": "aborted", "message": "Stopped by user"}
            logger.info(f"[Tidy] Force: context-manager completed, length={len(result)}")

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
                                        update_list.append((idx, content.strip()))
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

                # 确保 update 中的 idx 也在 keep 中
                update_idxs = {idx for idx, _ in update_list}
                missing_in_keep = update_idxs - keep_idxs
                if missing_in_keep:
                    logger.warning(f"[Tidy] Force: Adding update idxs to keep: {missing_in_keep}")
                    keep_idxs |= missing_in_keep

                # 计算删除列表：所有 idx - 保留 idx
                all_force_idxs = set(_f_idx_to_id.keys())
                delete_idxs = all_force_idxs - keep_idxs

                # 转换为 UUID
                deletes = [_f_idx_to_id[i] for i in sorted(delete_idxs) if i in _f_idx_to_id]
                updates = [
                    {"message_id": _f_idx_to_id[idx], "content": content}
                    for idx, content in update_list if idx in _f_idx_to_id
                ]
                # cursor 转换为 UUID
                if cursor_idx and cursor_idx in _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[cursor_idx]
                elif _f_idx_to_id:
                    new_compress_id = _f_idx_to_id[max(_f_idx_to_id.keys())]

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
                    except asyncio.TimeoutError:
                        logger.warning("[Tidy] Force: chat_lock 60s timeout, aborting execution")

                    if not _f_chat_lock_acquired:
                        _fq.resume()
                        raise RuntimeError("Force: chat_lock timeout")

                    if _fq._processing and _fq._processing_done.is_set():
                        pass
                    elif _fq._processing:
                        try:
                            await asyncio.wait_for(_fq._processing_done.wait(), timeout=30.0)
                        except asyncio.TimeoutError:
                            logger.warning("[Tidy] Force: ChatQueue processing timeout, aborting execution")
                            if _f_chat_lock_acquired:
                                _chat_lock.release()
                            _fq.resume()
                            raise RuntimeError("Force: ChatQueue processing timeout")

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
                    protected_force_ids: set[str] = set()
                    if protect_recent_count > 0:
                        _pids = []
                        for m in reversed(fresh_messages):
                            if getattr(m, "role", "") in ("user", "assistant"):
                                _pids.append(getattr(m, "id", ""))
                            if len(_pids) >= protect_recent_count:
                                break
                        protected_force_ids = set(_pids)
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
                                tcs = getattr(m, "tool_calls")
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

        else:
            logger.warning(f"[Tidy] Unknown mode: {mode}, skipping")
            return {"status": "error", "message": f"Unknown mode: {mode}. Use 'sleep' or 'force'."}

    except Exception as e:
        import traceback
        logger.error(f"[Tidy] Error: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e)}


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

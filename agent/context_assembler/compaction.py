"""批量压实（spec §3.6 / D14-D15）——纯机械、零 LLM、秒级。

压实动作：窗口重切（保留最近 N 个会话单元）→ 其余单元全量转指针块 →
索引行合并（超 30% 预算则最老相邻块合并为一行）→ D15 三轮硬约束
（先占位符化保留轮内旧工具输出，仍超减轮 3→2→1）→ 95% 应急终态
（保留轮全部工具输出占位符化 + 仅留最近 1 轮；仍超则放行 + error 日志）。

无损性：messages.db 真相源不动，任何时刻可全量重建。

已知边界（备案不改码，P3）：
- 会话中删消息导致指针块 end_rowid/rowid 区间与 DB 错位——不实时追踪，
  由启动一致性校验（integrity）自愈兜底；
- 应急终态与 D15 减轮等降级路径叠加时无组合检测，逐层各自判定——
  极端长会话下可能连续多轮触发降级叠加，属可接受盲区；
- 校准倍率按全集估算吸收工具输出开销，倍率漂移期间 80% 触发点存在
  提前/滞后偏差，由滞回闸门与 95% 应急线双重兜住。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from loguru import logger

from agent.context_assembler.blocks import (
    PointerBlock,
    default_db_path,
    load_all,
    upsert_blocks,
)
from agent.context_assembler.slicer import slice_units
from agent.context_manager import ContextManager

# 总量触发线 / 滞回复位线 / 应急警戒线（spec §3.6：95% 与 80% 间距即天然滞回）
TRIGGER_RATIO = 0.80
RESET_RATIO = 0.78
EMERGENCY_RATIO = 0.95
HARD_BUDGET_RATIO = 0.80   # D15 硬约束：压实后校准总量须回落到此线内
INDEX_RATIO_MAX = 0.30     # 历史索引 ≤30%（D2）

DEFAULT_KEEP_RECENT_TURNS = 3


def _read_keep_recent_turns() -> int:
    """读配置 context.keepRecentTurns（默认 3，§8 拍板项④）。"""
    try:
        from agent.subagent import _get_user_config_path
        config_path = _get_user_config_path()
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get("keepRecentTurns", DEFAULT_KEEP_RECENT_TURNS)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 1:
            return int(val)
        logger.warning(f"[Compaction] Invalid keepRecentTurns {val}, using default {DEFAULT_KEEP_RECENT_TURNS}")
    except Exception:
        pass
    return DEFAULT_KEEP_RECENT_TURNS


class CompactionGate:
    """滞回闸门：≥80% 触发并闩锁、<78% 复位——防倍率漂移在阈值附近反复触发。

    双触发去重：组装出口（估算驱动）与 runner 真值回调共用同一全局实例，
    同一轮次谁先拿到达线判定权谁压实，另一方被闩锁挡下。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latched = False

    def try_acquire(self, usage_ratio: float) -> bool:
        """usage_ratio 达触发线且未闩锁 → True（调用方执行压实）；否则 False。"""
        with self._lock:
            if self._latched:
                if usage_ratio < RESET_RATIO:
                    self._latched = False  # 水位已回落，解除闩锁
                return False
            if usage_ratio >= TRIGGER_RATIO:
                self._latched = True
                return True
            return False

    def release(self) -> None:
        """异常路径兜底：强制解除闩锁，避免压实失败后永久不再自动触发。"""
        with self._lock:
            self._latched = False


AUTO_GATE = CompactionGate()


# ---------------------------------------------------------------------------
# 指针块归档（与组装器同源逻辑的唯一实现，ContextManager 委托至此）
# ---------------------------------------------------------------------------

def archive_excluded_units(messages, units, window_start: int,
                           db_path: Path | None = None,
                           collect_entities: bool = True) -> int:
    """把窗口之外的完整会话单元机械写入指针块存储。返回新增块数。

    幂等：同 (start_msg_id, end_msg_id) 区间的块已存在则跳过。

    实体标签按块时间范围反查知识图谱（Task 4 接通，entity_tags.collect_tags）；
    失败降级为空标签，绝不阻塞归档。collect_entities=False 用于启动段整库重建
    （integrity）——此时 LightRAG 尚未 eager init，反查会触发阻塞式懒初始化。
    """
    p = Path(db_path) if db_path is not None else default_db_path()
    archived = load_all(p)
    existing_pairs = {(b.start_msg_id, b.end_msg_id) for b in archived}
    next_id = max((b.id for b in archived), default=0) + 1

    new_blocks: list[PointerBlock] = []
    for start, end in units:
        if start >= window_start:
            break  # 单元无缝有序，抵达窗口起点后全部在窗内
        pair = (messages[start].id, messages[end].id)
        if pair in existing_pairs:
            continue
        first_user = next(
            (m.content for m in messages[start : end + 1] if m.role == "user"), ""
        )
        new_blocks.append(
            PointerBlock(
                id=next_id,
                start_msg_id=pair[0],
                end_msg_id=pair[1],
                start_rowid=messages[start].rowid,
                end_rowid=messages[end].rowid,
                count=end - start + 1,
                time_start=messages[start].created_at,
                time_end=messages[end].created_at,
                entities=[],
                first_user=first_user,
            )
        )
        next_id += 1

    if new_blocks:
        # 实体标签批量反查（单次图快照服务全部新块；失败=空标签不阻塞）
        try:
            from agent.context_assembler.entity_tags import collect_tags
            tags_list = (
                collect_tags([(b.time_start, b.time_end) for b in new_blocks])
                if collect_entities
                else [[] for _ in new_blocks]
            )
        except Exception as e:
            logger.debug(f"[Compaction] entity tags degraded to empty: {e}")
            tags_list = [[] for _ in new_blocks]
        for b, tags in zip(new_blocks, tags_list):
            b.entities = tags

        upsert_blocks(new_blocks, p)
    return len(new_blocks)


# ---------------------------------------------------------------------------
# 索引渲染（含超 30% 预算时的最老相邻块合并）
# ---------------------------------------------------------------------------

def _short_date(ts: str) -> str:
    return ContextManager._short_date(ts)


def _group_line(lo: int, hi: int, count: int, t0: str, t1: str, first_user: str,
                entities: list[str] | None = None) -> str:
    ids = f"块#{lo}~{hi}" if hi > lo else f"块#{lo}"
    dates = f"{_short_date(t0)}~{_short_date(t1)}" if t0 or t1 else ""
    parts = [f"[{ids}]", dates, f"{count}条"]
    if entities:
        parts.append("实体:" + "/".join(entities[:3]))
    parts.append(f'首问:"{first_user}"')
    return " · ".join(p for p in parts if p)


def render_index_grouped(blocks: list[PointerBlock], budget_tokens: int,
                         count_fn) -> str:
    """渲染索引前导消息内容；超预算时自最老端起两两合并相邻行为一行。

    合并仅发生在渲染层（存储块保持独立，read_history_block 仍可按单块号取回）。
    """
    if not blocks:
        return ""
    header = (
        f"[历史索引]\n"
        f"共 {len(blocks)} 块早期对话已归档，可用 read_history_block 工具按块号取回原文。"
    )

    # 组初始状态：每块一行
    groups: list[list] = [
        [b.id, b.id, b.count, b.time_start, b.time_end, b.first_user,
         list(getattr(b, "entities", ()) or ())] for b in blocks
    ]

    def render(groups: list[list]) -> str:
        lines = [_group_line(*g) for g in groups]
        return header + "\n" + "\n".join(lines)

    def token_cost(text: str) -> int:
        return count_fn([{"role": "user", "content": text}])

    text = render(groups)
    while len(groups) > 1 and token_cost(text) > budget_tokens:
        merged_entities = list(dict.fromkeys(groups[0][6] + groups[1][6]))[:3]
        merged = [
            groups[0][0], groups[1][1],
            groups[0][2] + groups[1][2],
            groups[0][3], groups[1][4],
            groups[0][5],
            merged_entities,
        ]
        groups = [merged] + groups[2:]
        text = render(groups)
    return text


# ---------------------------------------------------------------------------
# 核心压实
# ---------------------------------------------------------------------------

def _placeholderize_all_tool_outputs(messages: list[dict]) -> int:
    """把列表内全部 tool 输出替换为占位符（含最近一轮，95% 应急终态专用）。

    复用 agent_loop 的占位符结构语义（幂等判定/工具名回查），但不复用其
    保护边界逻辑——_placeholderize_tool_outputs 的「保护不足默认全保护」
    语义无法表达零保护。
    """
    from agent.generic.agent_loop import (
        _find_tool_name_from_assistant,
        _is_tool_placeholder,
    )
    replaced = 0
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        if _is_tool_placeholder(content):
            continue  # 幂等：已占位符化，跳过
        name = m.get("name", "") or ""
        if not name:
            name = _find_tool_name_from_assistant(messages, i, m.get("tool_call_id", "") or "")
        m["content"] = f"[{name} 输出已裁剪]" if name else "[输出已裁剪]"
        replaced += 1
    return replaced


def build_compact_view(messages, *, system_msg: dict | None = None,
                       keep_turns: int | None = None,
                       blocks_db_path=None,
                       context_window_tokens: int | None = None):
    """对消息全集做批量压实，返回 (新视图 dict 列表, 统计 dict)。

    新视图组成（R2-A P1 定案）：[system 消息原样（可含 cache_control）]
    + [索引前导 user 消息] + [窗口消息 dict 列表]。system_msg=None 时省略首段
    （组装出口场景由 agent_loop 自行拼装 system）。

    messages 为 Message 对象序列（需 id/rowid/role/content/tool_calls/
    tool_call_id/created_at），与 MessageStore.get_messages 输出一致。
    """
    from agent.context_assembler import calibration  # noqa: E402（惰性导入防环）
    from agent.generic.agent_loop import _placeholderize_tool_outputs  # noqa: E402

    db_path = Path(blocks_db_path) if blocks_db_path is not None else default_db_path()
    if context_window_tokens:
        cw = int(context_window_tokens)
    else:
        from agent.subagent import _read_context_window_tokens
        cw = _read_context_window_tokens()

    hard_budget = max(1, int(cw * HARD_BUDGET_RATIO))
    emergency_line = int(cw * EMERGENCY_RATIO)

    converted = [ContextManager._message_to_dict(m) for m in messages]
    units = slice_units(messages)
    n_units = len(units)

    keep = _read_keep_recent_turns() if keep_turns is None else max(1, int(keep_turns))
    if n_units:
        keep = min(keep, n_units)

    sys_est = ContextManager.count_tokens_simple([system_msg]) if system_msg else 0

    # 循环前的既有块集合用于预算估算（归档后索引会略增，最终重算兜底）
    existing_blocks = load_all(db_path)

    def window_slice(k: int) -> list[dict]:
        start_idx = units[n_units - k][0] if n_units else len(converted)
        return [e for e in converted[start_idx:] if e is not None]

    def total_est(window: list[dict]) -> float:
        fixed = sys_est + ContextManager.count_tokens_simple(
            [{"role": "user", "content": render_index_grouped(existing_blocks, cw, ContextManager.count_tokens_simple)}]
        ) if existing_blocks else sys_est
        return calibration.estimate(
            ContextManager.count_tokens_simple(window) + fixed
        )

    tools_replaced = 0
    placeholderized = False
    if n_units:
        window = window_slice(keep)
        est = total_est(window)
        # D15 主循环：先占位符化保留轮内旧工具输出（保护最近一轮），仍超减轮 k→k-1
        while est > hard_budget:
            if not placeholderized:
                ratio = calibration.get_ratio()
                target = int(max(0, hard_budget / ratio - sys_est))
                replaced = _placeholderize_tool_outputs(window, target, protect_turns=1)
                tools_replaced += replaced
                placeholderized = True
                if replaced:
                    est = total_est(window)
                    continue
            if keep > 1:
                keep -= 1
                window = window_slice(keep)
                est = total_est(window)
            else:
                break

        # 95% 应急终态（spec §3.6）：保留轮全部工具输出占位符化 + 仅留最近 1 轮
        if est >= emergency_line:
            logger.error(f"[Compaction] Emergency terminal state engaged: est={est:.0f} >= "
                         f"95% of {cw}")
            tools_replaced += _placeholderize_all_tool_outputs(window)
            if keep > 1:
                keep = 1
                window = window_slice(keep)
            est = total_est(window)
            if est >= emergency_line:
                logger.error(f"[Compaction] Still >=95% after emergency terminal state "
                             f"(est~{est:.0f}/{cw}) — releasing oversized context to server-side "
                             f"error/degradation path")
    else:
        window = []
        est = float(sys_est)

    # 收敛后按最终窗口起点归档（幂等）；再取全量块渲染索引（含合并）
    archived_count = 0
    if n_units:
        window_start = units[n_units - keep][0]
        archived_count = archive_excluded_units(messages, units, window_start, db_path)

    blocks = load_all(db_path)
    index_text = render_index_grouped(blocks, int(cw * INDEX_RATIO_MAX),
                                      ContextManager.count_tokens_simple)
    view: list[dict] = []
    if system_msg is not None:
        view.append(system_msg)
    if index_text:
        view.append({"role": "user", "content": index_text})
    view.extend(window)

    final_est = calibration.estimate(
        ContextManager.count_tokens_simple(view)
    )
    stats = {
        "usage": (final_est / cw) if cw else None,
        "tokens_estimate": int(final_est),
        "context_window": cw,
        "keep_turns": keep,
        "units_total": n_units,
        "blocks_archived": archived_count,
        "blocks_total": len(blocks),
        "tools_placeholderized": tools_replaced,
        "emergency": est >= emergency_line if cw else False,
    }
    return view, stats


async def compact_now(store, system_msg: dict | None = None, *,
                      keep_turns: int | None = None,
                      blocks_db_path=None,
                      context_window_tokens: int | None = None) -> list[dict]:
    """压实入口（ticket 契约签名）：读 store 全量消息 → 强制重切 → 返回新视图。

    与 get_context_for_chat 同源逻辑（slice_units 切割 + 指针块归档 + 索引渲染），
    但不做 80% 触发判定——手动 /compact 与溢出收编路径直达。
    需要统计信息（如圆环 usage 回传）的调用方改用 compact_now_detailed。
    """
    view, _stats = await compact_now_detailed(
        store, system_msg=system_msg, keep_turns=keep_turns,
        blocks_db_path=blocks_db_path, context_window_tokens=context_window_tokens,
    )
    return view


async def compact_now_detailed(store, system_msg: dict | None = None, *,
                               keep_turns: int | None = None,
                               blocks_db_path=None,
                               context_window_tokens: int | None = None):
    """同 compact_now，另返回统计 dict（usage/blocks/tools 等）。"""
    messages = await store.get_messages(limit=None)
    return build_compact_view(
        messages, system_msg=system_msg, keep_turns=keep_turns,
        blocks_db_path=blocks_db_path, context_window_tokens=context_window_tokens,
    )

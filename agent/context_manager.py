"""
上下文管理器 - 统一历史管理职责

职责：
1. 从 MessageStore 加载全量历史消息（DB 真相源，永不删改）
2. 转换消息格式（Message → dict）
3. 组装「历史索引 + 近期原文窗口」视图：被挤出窗口的完整会话单元
   机械归档为指针块（agent/context_assembler），旧压缩语义已退役

架构：
MessageStore (持久化) → ContextManager (管理) → agent_loop (使用)
"""

import re
import sys
from pathlib import Path
from typing import Any

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.context_assembler.blocks import PointerBlock, default_db_path, load_all
from agent.context_assembler.slicer import slice_units
from agent.session import MessageStore
from agent.subagent import _read_context_window_tokens, _read_warning_threshold

# 原文窗口预算占上下文窗口的比例（spec D2 分区预算；校准倍率接入在 Task 3）
_WINDOW_BUDGET_RATIO = 0.5
_INDEX_ENTITY_MAX = 3  # 索引行实体标签上限（spec §3.3）
_SUMMARY_MAX_CHARS = 100  # 摘要行尺寸不变式：≤100 字（spec §3.3 可选增强）


class ContextManager:
    """上下文管理器 - 统一历史管理职责"""

    def __init__(
        self,
        message_store: MessageStore,
        max_messages: int = 0,
        max_tokens: int = 0,
        blocks_db_path: Path | None = None,
    ):
        """
        初始化上下文管理器

        Args:
            message_store: 消息存储实例
            max_messages: 最大消息数量（默认0=不限制）
            max_tokens: 最大 token 数量（0 表示从配置读取）
            blocks_db_path: 指针块存储 DB 路径（默认 ~/.niu/context_blocks.db，测试可注入临时路径）
        """
        if max_tokens <= 0:
            max_tokens = _read_context_window_tokens()
        self.store = message_store
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self._warning_threshold = _read_warning_threshold()
        self._blocks_db_path = Path(blocks_db_path) if blocks_db_path is not None else None
        # 最近一次组装的 system 消息 token 数（Task 3：80% 触发判定的系统侧输入；
        # 由 runner 组装 system 后回填，缺省 0=未知，首轮回退偏保守）
        self._system_token_estimate = 0

    @staticmethod
    def _message_to_dict(msg) -> dict[str, Any] | None:
        """Message → agent_loop 消息 dict；完全空的消息返回 None。"""
        entry = {"role": msg.role, "content": msg.content or ""}

        # 还原 tool_calls（assistant 消息可能携带工具调用）
        if msg.tool_calls:
            entry["tool_calls"] = msg.tool_calls

        # 还原 tool_call_id（tool 消息必须关联到对应的 tool_call）
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id

        # 完全空的消息（无 content、无 tool_calls、无 tool_call_id）跳过
        if not msg.content and not msg.tool_calls and not msg.tool_call_id:
            return None
        return entry

    async def load_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """
        加载历史消息并转换为 agent_loop 格式

        完整还原 tool 消息：保留 tool_calls、tool_call_id 等字段。

        Args:
            limit: 加载消息数量，None 则使用 max_messages

        Returns:
            消息列表 [{"role": "user/assistant/tool", "content": str, ...}, ...]
        """
        if limit is None or limit <= 0:
            limit = None  # None = 不限制，返回全部消息

        # 从 MessageStore 加载
        messages = await self.store.get_messages(limit=limit)

        history = []
        for msg in messages:
            entry = self._message_to_dict(msg)
            if entry is not None:
                history.append(entry)

        return history

    @staticmethod
    def count_tokens_simple(messages: list[dict[str, Any]]) -> int:
        """
        使用 TokenCalculator 计算 token 数量

        回退到字符数估算（约 2 字符/token，偏保守避免低估）。

        Args:
            messages: 消息列表

        Returns:
            token 数量
        """
        try:
            from agent.token_calculator import TokenCalculator
            return TokenCalculator.get().count_messages(messages)
        except Exception:
            # 回退：约 2 字符/token（偏保守，避免低估导致不触发压缩）
            total_tokens = 0
            for msg in messages:
                content = msg.get("content", "")
                total_tokens += max(1, len(content) // 2) + 4
            return total_tokens

    @staticmethod
    def _short_date(ts: str) -> str:
        """ISO created_at → MM-DD；非预期格式返回空串。"""
        if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
            return ts[5:10]
        return ""

    @staticmethod
    def _render_index(blocks: list[PointerBlock]) -> str:
        """渲染索引前导消息内容（纯时间线 FIFO，spec §3.3）。"""
        lines = [
            "[历史索引]",
            f"共 {len(blocks)} 块早期对话已归档，可用 read_history_block 工具按块号取回原文。",
        ]
        for b in blocks:
            # 摘要行替代机械行（spec §3.3/D6 可选增强）：summary_state=done 且摘要
            # 非空时用摘要行替代——块号句柄保留，≤100 字，索引区尺寸不变式仍成立；
            # pending/失败块保持机械行（兜底主路径）
            if getattr(b, "summary_state", "") == "done" and (b.summary or "").strip():
                summary = re.sub(r"\s+", " ", b.summary).strip()[:_SUMMARY_MAX_CHARS]
                lines.append(f"[块#{b.id}] {summary}")
                continue
            # 实体标签（spec §3.3 机械成分，≤3 个；无标签时省略该段）
            tags = list(getattr(b, "entities", ()) or ())[:_INDEX_ENTITY_MAX]
            entity_part = " · 实体:" + "/".join(tags) if tags else ""
            lines.append(
                f'[块#{b.id}] {ContextManager._short_date(b.time_start)}'
                f'~{ContextManager._short_date(b.time_end)} · {b.count}条{entity_part}'
                f' · 首问:"{b.first_user}"'
            )
        return "\n".join(lines)

    def _archive_excluded_units(self, messages, units, window_start: int) -> int:
        """把窗口之外的完整会话单元机械写入指针块存储（委托 compaction 单一实现）。

        幂等：同 (start_msg_id, end_msg_id) 区间的块已存在则跳过。
        """
        from agent.context_assembler.compaction import archive_excluded_units
        return archive_excluded_units(
            messages, units, window_start,
            self._blocks_db_path or default_db_path(),
        )

    def set_system_token_estimate(self, n_tokens: int) -> None:
        """记录最近一次组装的 system 消息 token 数（80% 触发判定的系统侧输入）。"""
        self._system_token_estimate = max(0, int(n_tokens))

    async def get_context_for_chat(self, exclude_last: bool = True) -> list[dict[str, Any]]:
        """
        获取用于聊天的上下文（主入口）——组装器视图

        流程：
        1. 读 DB 全量消息（真相源不动）；exclude_last 时排除最后一条（当前输入）
        2. 会话单元切割 → 从最新单元向前累加装填原文窗口
           （预算 = contextWindowSize × 50%，本期固定值，倍率接入在 Task 3）
        3. 被挤出的完整单元机械归档为指针块（幂等）
        4. 输出 = [索引前导 user 消息] + [窗口消息 dict 列表]；无归档块时省略索引消息

        窗口起点恒为会话单元边界（tool_calls 配对完整）；dict 形态与 load_history 一致。

        Args:
            exclude_last: 是否排除最后一条消息（当前用户输入）

        Returns:
            消息列表 [{"role": ..., "content": ..., ...}, ...]
        """
        messages = await self.store.get_messages(limit=None)
        if exclude_last and messages:
            messages = messages[:-1]

        units = slice_units(messages)
        converted = [self._message_to_dict(m) for m in messages]
        budget = int(self.max_tokens * _WINDOW_BUDGET_RATIO)

        # 窗口装填：从最新单元向前累加，装不下即止（保底最新一个单元恒在窗内）
        window_start = len(messages)
        total = 0
        included = 0
        for start, end in reversed(units):
            unit_entries = [e for e in converted[start : end + 1] if e is not None]
            unit_cost = self.count_tokens_simple(unit_entries)
            if included and total + unit_cost > budget:
                break
            window_start = start
            total += unit_cost
            included += 1

        # 被挤出的完整单元 → 指针块归档（幂等）
        if units and window_start < len(messages):
            self._archive_excluded_units(messages, units, window_start)

        # 输出视图：索引前导 + 窗口原文
        view: list[dict[str, Any]] = []
        index_blocks = load_all(self._blocks_db_path or default_db_path())
        if index_blocks:
            view.append({"role": "user", "content": self._render_index(index_blocks)})
        view.extend(e for e in converted[window_start:] if e is not None)

        # Task 3：组装出口 80% 触发检查——校准后总量估算 ≥80% 即地压实（D14）。
        # 滞回（≥80% 触发 / <78% 复位）与 runner 真值回调共用 AUTO_GATE，双触发去重不双压。
        try:
            from agent.context_assembler import calibration
            from agent.context_assembler.compaction import AUTO_GATE, build_compact_view

            est = calibration.estimate(
                self.count_tokens_simple(view) + self._system_token_estimate
            )
            usage_ratio = est / self.max_tokens if self.max_tokens else 0.0
            if AUTO_GATE.try_acquire(usage_ratio):
                logger.info(
                    f"[Context] Calibrated usage {est:.0f}/{self.max_tokens} "
                    f"({usage_ratio:.1%}) >= 80%, compacting at assembly exit"
                )
                new_view, stats = build_compact_view(
                    messages,
                    system_msg=None,
                    blocks_db_path=self._blocks_db_path,
                    context_window_tokens=self.max_tokens,
                )
                logger.info(
                    f"[Context] Compacted at assembly exit: keep_turns={stats['keep_turns']}, "
                    f"blocks_archived={stats['blocks_archived']}, "
                    f"tools_placeholderized={stats['tools_placeholderized']}, "
                    f"usage_after={stats['usage']}"
                )
                return new_view
        except Exception as e:
            from agent.context_assembler import compaction as _comp
            _comp.AUTO_GATE.release()  # 压实失败解除闩锁，避免永久不再自动触发
            logger.warning(f"[Context] Assembly-exit compaction failed, using un-compacted view: {e}")
        return view


# 全局实例管理
_context_manager: ContextManager | None = None


async def get_context_manager(message_store: MessageStore | None = None) -> ContextManager:
    """
    获取全局 ContextManager 实例

    Args:
        message_store: 消息存储实例（首次调用时需要）

    Returns:
        ContextManager 实例
    """
    global _context_manager

    if _context_manager is None:
        if message_store is None:
            raise ValueError("First call requires message_store parameter")
        _context_manager = ContextManager(message_store)

    return _context_manager


def reset_context_manager():
    """重置全局实例（用于测试）"""
    global _context_manager
    _context_manager = None

def peek_context_manager() -> ContextManager | None:
    """同步读取全局实例（未初始化返回 None）。
    供 runner 在组装 system 后回填 set_system_token_estimate 用——
    get_context_manager 是 async 且首次调用需要 message_store，回填场景两者都不适用。
    """
    return _context_manager

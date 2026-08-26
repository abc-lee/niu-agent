"""
上下文管理器 - 统一历史管理职责

职责：
1. 从 MessageStore 加载全量历史消息（DB 真相源，永不删改）
2. 转换消息格式（Message → dict）
3. 水位线组装：候选 = DB 全量中未被任何块覆盖的尾部消息——不做预算装填、
   不在组装路径归档；归档只发生在批量压实（80% 触发）与整库重建
   （agent/context_assembler.integrity），旧压缩语义已退役

架构：
MessageStore (持久化) → ContextManager (管理) → agent_loop (使用)
"""

import sys
from pathlib import Path
from typing import Any

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))


from agent.context_assembler.blocks import PointerBlock, default_db_path, load_all
from agent.session import MessageStore
from agent.subagent import _read_context_window_tokens, _read_warning_threshold

_INDEX_ENTITY_MAX = 3  # 索引行实体标签上限（spec §3.3）


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
    def _watermark_split(blocks: list[PointerBlock], messages) -> tuple[int, int]:
        """水位线切割：返回 (最后一个被覆盖消息的下标, 已覆盖前缀中意外未覆盖条数)。

        块表存储粒度为端点（start/end msg_id + rowid，见 blocks.py schema）——
        会话单元内消息连续且 DB append-only，rowid ∈ [start_rowid, end_rowid]
        的区间覆盖即成员覆盖。取舍：不补存成员 id 列表（最小改动；一致性校验
        ③④ 的区间单调/count 核对已保证区间可靠，补列属冗余 schema 变更）。
        """
        intervals = sorted((b.start_rowid, b.end_rowid) for b in blocks)
        merged: list[list[int]] = []
        for lo, hi in intervals:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])

        def is_covered(rowid: int) -> bool:
            # 区间数极小，线性扫足够；bisect 无必要
            return any(lo <= rowid <= hi for lo, hi in merged)

        last_covered = -1
        for i, m in enumerate(messages):
            if is_covered(m.rowid):
                last_covered = i
        stragglers = sum(
            1 for m in messages[: last_covered + 1] if not is_covered(m.rowid)
        )
        return last_covered, stragglers

    @staticmethod
    def _render_index(blocks: list[PointerBlock]) -> str:
        """渲染索引前导消息内容（纯时间线 FIFO，spec §3.3）。"""
        lines = [
            "[历史索引]",
            f"共 {len(blocks)} 块早期对话已归档，可用 read_history_block 工具按块号取回原文。",
        ]
        for b in blocks:
            # 实体标签（spec §3.3 机械成分，≤3 个；无标签时省略该段）
            tags = list(getattr(b, "entities", ()) or ())[:_INDEX_ENTITY_MAX]
            entity_part = " · 实体:" + "/".join(tags) if tags else ""
            lines.append(
                f'[块#{b.id}] {ContextManager._short_date(b.time_start)}'
                f'~{ContextManager._short_date(b.time_end)} · {b.count}条{entity_part}'
                f' · 首问:"{b.first_user}"'
            )
        return "\n".join(lines)

    def set_system_token_estimate(self, n_tokens: int) -> None:
        """记录最近一次组装的 system 消息 token 数（80% 触发判定的系统侧输入）。"""
        self._system_token_estimate = max(0, int(n_tokens))

    async def get_context_for_chat(self, exclude_last: bool = True) -> list[dict[str, Any]]:
        """
        获取用于聊天的上下文（主入口）——组装器视图（水位线模型）

        流程：
        1. 读 DB 全量消息（真相源不动）+ 块库全量块
        2. 候选消息 = 未被任何块覆盖的消息（append-only 保证连续位于尾部；
           意外不连续时取最后一个被覆盖消息之后的部分并告警）
        3. exclude_last 时排除候选末条（当前输入）
        4. 输出 = [索引前导 user 消息（仅当有块）] + [候选消息原文]
           ——不做预算装填、不在组装路径归档。两次压实之间上下文自然增长
           是 D14 设计内行为，由 80% 触发压实收口（压实后水位线前移，
           下轮组装只剩保留轮+新增量）

        候选起点恒为块边界（=会话单元边界，tool_calls 配对完整）；dict 形态
        与 load_history 一致。

        Args:
            exclude_last: 是否排除最后一条消息（当前用户输入）

        Returns:
            消息列表 [{"role": ..., "content": ..., ...}, ...]
        """
        messages = await self.store.get_messages(limit=None)

        candidates = messages
        blocks = load_all(self._blocks_db_path or default_db_path())
        if blocks and messages:
            last_covered, stragglers = self._watermark_split(blocks, messages)
            if stragglers:
                logger.warning(
                    f"[Context] {stragglers} 条未覆盖消息出现在已覆盖前缀中"
                    f"（append-only 被破坏），取最后一个被覆盖消息之后的尾部为候选"
                )
            if last_covered >= 0:
                candidates = messages[last_covered + 1:]

        history = candidates[:-1] if exclude_last and candidates else candidates

        # 输出视图：索引前导（仅当有归档块）+ 候选原文
        view: list[dict[str, Any]] = []
        if blocks:
            view.append({"role": "user", "content": self._render_index(blocks)})
        view.extend(
            e for e in (self._message_to_dict(m) for m in history) if e is not None
        )

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
                    history,
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
                # 压实成功即复位闩锁：压实后视图常落 [78%,80%)，滞回 <78% 复位线
                # 永不满足——不复位则自动压实进程级失效（P1 修复）
                AUTO_GATE.release()
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

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

import json
import sys
from pathlib import Path
from typing import Any

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))


from agent.context_assembler.blocks import PointerBlock, default_db_path, load_all
from agent.session import MessageStore, fold_columns_available
from agent.subagent import _read_context_window_tokens, _read_warning_threshold

_INDEX_ENTITY_MAX = 3  # 索引行实体标签上限（spec §3.3）
TRIGGER_RATIO_FALLBACK = 0.80  # compaction 导入失败时的仪表盘兜底线（=compaction.TRIGGER_RATIO 默认值引用）


def build_tc_map(messages) -> dict[str, tuple[str, str]]:
    """从 assistant 消息 tool_calls 构建 tool_call_id → (工具名, 参数摘要≤80字符) 映射。

    ⚠ tool_calls 是 OpenAI 嵌套格式 {id, type, function:{name, arguments}}——
    必须读 tc["function"]["name"] / tc["function"]["arguments"]，tc["name"] 恒为 None。
    参数摘要纯截断（≤80 字符）——固化不变式：同一消息每轮渲染逐字节一致（缓存友好）。
    """
    tc_map: dict[str, tuple[str, str]] = {}
    for m in messages:
        if getattr(m, "role", None) != "assistant":
            continue
        for tc in getattr(m, "tool_calls", None) or []:
            if not isinstance(tc, dict) or not tc.get("id"):
                continue
            fn = tc.get("function")
            name = fn.get("name", "") if isinstance(fn, dict) else ""
            args = fn.get("arguments", "") if isinstance(fn, dict) else ""
            if isinstance(args, (dict, list)):
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = str(args)
            tc_map[tc["id"]] = (name or "", str(args)[:80])
    return tc_map


def render_tool_content(msg, tc_map) -> str:
    """tool 消息 content 统一渲染（常规组装与压实路径共用，spec §4）。

    - 非 tool / rowid<=0 → 原样返回
    - tc_map is None（历史回放路径 load_history）→ 不渲染头行/占位符，原样返回
    - fold_columns_available() False（迁移失败降级）→ 原样返回
    - folded=1 → 完成态占位符（含"已由 fold_tool_output 折叠：工具名(参数摘要≤80字符，
      无配对 unknown)"+"原占约 X%"；pct None 时省略占比分句），以「获取]」收尾兼容
      agent_loop._is_tool_placeholder 识别（防应急 ToolCrop 覆盖）——工具名+参数摘要是
      LLM 重新调用原工具取回原文的通道（spec §4）
    - folded=0 → 头行 + 原文；编号(rowid)/工具名/pct 全部固化来源，逐字节稳定
    """
    content = msg.content or ""
    if msg.role != "tool" or getattr(msg, "rowid", 0) <= 0:
        return content
    if tc_map is None:
        return content
    if not fold_columns_available():
        return content
    name, args = tc_map.get(msg.tool_call_id or "", ("", ""))
    name = name or "unknown"
    pct = getattr(msg, "output_pct", None)
    rowid = msg.rowid
    if getattr(msg, "folded", 0):
        pct_part = f"（原占约 {pct}%）" if pct is not None else ""
        return (f"[输出#{rowid} 已由 fold_tool_output 折叠：{name}({args})，本条已移出上下文{pct_part}。"
                f"如需原文请重新调用原工具获取]")
    header = f"[输出#{rowid} · {name}" + (f" · 占上下文 {pct}%]" if pct is not None else "]")
    return f"{header}\n{content}"


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
        # fold 仪表盘缓存（spec §5）：最近一次组装的 usage + 窗口未折叠 tool 输出统计；
        # None=尚未组装过。压实轮沿用压实前统计（高估一轮，下轮自愈——R2-A P3 记录接受）
        self._fold_stats = None

    @staticmethod
    def _message_to_dict(msg, tc_map=None) -> dict[str, Any] | None:
        """Message → agent_loop 消息 dict；完全空的消息返回 None。

        tool 消息 content 经 render_tool_content 处理（tc_map=None=不渲染——
        历史回放路径；get_context_for_chat / build_compact_view 传 map 同制式）。
        """
        entry = {"role": msg.role, "content": render_tool_content(msg, tc_map)}

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
            f"共 {len(blocks)} 块早期对话已归档，可用 read_history_block 工具按行首方括号内数字取回原文。",
        ]
        for b in blocks:
            # 实体标签（spec §3.3 机械成分，≤3 个；无标签时省略该段）
            tags = list(getattr(b, "entities", ()) or ())[:_INDEX_ENTITY_MAX]
            entity_part = " · 实体:" + "/".join(tags) if tags else ""
            d0 = ContextManager._short_date(b.time_start)
            d1 = ContextManager._short_date(b.time_end)
            date_part = d0 if d0 == d1 else f"{d0}~{d1}"  # 起止同日坍缩为单日期
            lines.append(
                f'[{b.id}] {date_part} · {b.count}条{entity_part}'
                f' · 首问:"{b.first_user}"'
            )
        return "\n".join(lines)

    def set_system_token_estimate(self, n_tokens: int) -> None:
        """记录最近一次组装的 system 消息 token 数（80% 触发判定的系统侧输入）。"""
        self._system_token_estimate = max(0, int(n_tokens))

    def assemble_view_sync(self, db_messages, exclude_last: bool = True) -> list[dict]:
        """同步纯组装视图（入口/折叠刷新共用，单一渲染源）。

        输入 Message 对象序列 → [索引前导(有块时)]+[候选原文折叠渲染]；更新 _fold_stats
        （usage 存校准值）。不含压实尾段——rebuild 不得触发压实（深审 P1：
        会把刚折叠的目标行归档移出窗口）。
        注：blocks 在本函数 load_all 一次；包装层压实尾段为取候选 Message 序列会再
        load 一次（两 load 间无写者——单线程、压实是唯一写者且在两者之后，零漂移，
        R4-A P3-1）。

        Args:
            db_messages: DB 全量 Message 对象序列（真相源不动）
            exclude_last: 是否排除候选末条（当前用户输入）

        Returns:
            消息列表 [{"role": ..., "content": ..., ...}, ...]
        """
        candidates = db_messages
        blocks = load_all(self._blocks_db_path or default_db_path())
        if blocks and db_messages:
            last_covered, stragglers = self._watermark_split(blocks, db_messages)
            if stragglers:
                logger.warning(
                    f"[Context] {stragglers} 条未覆盖消息出现在已覆盖前缀中"
                    f"（append-only 被破坏），取最后一个被覆盖消息之后的尾部为候选"
                )
            if last_covered >= 0:
                candidates = db_messages[last_covered + 1:]

        history = candidates[:-1] if exclude_last and candidates else candidates

        # fold 仪表盘统计（spec §5）：n=窗口内全部未折叠 tool 消息（含 NULL pct 旧数据——
        # 它们同样可折）；m/p=有快照者条数与合计。迁移失败降级时 n=None → 仪表盘省略该段
        n = m = 0
        p = 0.0
        if fold_columns_available():
            for msg in history:
                if getattr(msg, "role", None) != "tool" or getattr(msg, "folded", 0):
                    continue
                n += 1
                pct = getattr(msg, "output_pct", None)
                if pct is not None:
                    m += 1
                    p += pct
        self._fold_stats = {
            "n": n if fold_columns_available() else None,
            "m": m,
            "p": round(p, 1),
            "usage": None,
        }

        # 输出视图：索引前导（仅当有归档块）+ 候选原文
        view: list[dict[str, Any]] = []
        if blocks:
            view.append({"role": "user", "content": self._render_index(blocks)})
        # fold 渲染（spec §4）：窗口 tool 消息头行/折叠占位符——tc_map 从窗口内
        # assistant tool_calls 提取（候选起点恒为会话单元边界，配对完整）
        tc_map = build_tc_map(history)
        view.extend(
            e for e in (self._message_to_dict(m, tc_map) for m in history) if e is not None
        )

        # usage：raw → 校准覆写（R1/R4 定案：校准随计算移入本函数——rebuild 与入口同口径；
        # 纯函数 n×get_ratio 无副作用）。降级时保留 raw + debug 一行（对齐现状可观测性）
        base_est = self.count_tokens_simple(view) + self._system_token_estimate
        usage = base_est / self.max_tokens if self.max_tokens else 0.0
        try:
            from agent.context_assembler import calibration
            usage = calibration.estimate(base_est) / self.max_tokens if self.max_tokens else 0.0
        except Exception as e:
            logger.debug(f"[Context] calibration failed, usage falls back to raw: {e}")
        self._fold_stats["usage"] = usage
        return view

    async def get_context_for_chat(self, exclude_last: bool = True) -> list[dict[str, Any]]:
        """
        获取用于聊天的上下文（主入口）——组装器视图（水位线模型）

        流程：读 DB 全量消息 → assemble_view_sync 纯组装（水位线切分 + fold 统计
        + 索引 + 折叠渲染 + 校准后 usage 估算）→ 压实尾段（本包装层独有，D14）。

        候选消息 = 未被任何块覆盖的消息（append-only 保证连续位于尾部；意外不连续时
        取最后一个被覆盖消息之后的部分并告警——告警在 assemble_view_sync 内发出）；
        exclude_last 时排除候选末条（当前输入）。输出 = [索引前导 user 消息（仅当有块）]
        + [候选消息原文]——不做预算装填、不在组装路径归档。两次压实之间上下文自然增长
        是 D14 设计内行为，由触发线压实收口（压实后水位线前移，下轮组装只剩保留轮+新增量）。

        候选起点恒为块边界（=会话单元边界，tool_calls 配对完整）；dict 形态
        与 load_history 一致。

        Args:
            exclude_last: 是否排除最后一条消息（当前用户输入）

        Returns:
            消息列表 [{"role": ..., "content": ..., ...}, ...]
        """
        messages = await self.store.get_messages(limit=None)
        view = self.assemble_view_sync(messages, exclude_last)

        # Task 3：组装出口触发线检查——校准后总量估算达线即地压实（D14）。
        # 滞回（≥trigger 触发 / <trigger−0.02 复位）与 runner 真值回调共用 AUTO_GATE，双触发去重不双压。
        # 压实需候选 Message 序列：_watermark_split 无状态重算（与 assemble_view_sync 内
        # 同源——同一 static 方法同输入零漂移，未来维护漂移有界自愈，R2-A P3a）；
        # stragglers warning 已在 assemble_view_sync 内发出一次，此处不重复（R5-A P3-2）。
        candidates = messages
        blocks = load_all(self._blocks_db_path or default_db_path())
        if blocks and messages:
            last_covered, _stragglers = self._watermark_split(blocks, messages)
            if last_covered >= 0:
                candidates = messages[last_covered + 1:]
        # exclude_last 显式切片（R2-A P2：漏切则入口压实时当前用户输入重复进 prompt）
        history = candidates[:-1] if exclude_last and candidates else candidates

        base_est = self.count_tokens_simple(view) + self._system_token_estimate
        try:
            from agent.context_assembler import calibration
            from agent.context_assembler.compaction import AUTO_GATE, build_compact_view

            # AUTO_GATE 判定不读 _fold_stats['usage']——try 内自行重算校准值（纯函数双调无害；
            # 若读 _fold_stats，calibration 降级 raw 时会以 raw 调 try_acquire，破"入口行为零变化"。
            # 现状语义保持：calibration 抛错时异常先于 try_acquire 终止、从不尝试压实）
            est = calibration.estimate(base_est)
            usage_ratio = est / self.max_tokens if self.max_tokens else 0.0
            if AUTO_GATE.try_acquire(usage_ratio):
                logger.info(
                    f"[Context] Calibrated usage {est:.0f}/{self.max_tokens} "
                    f"({usage_ratio:.1%}) >= trigger line, compacting at assembly exit"
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
                # 压实成功即复位闩锁：压实后视图常落 [复位线, 触发线) 滞回带内，
                # 不复位则自动压实进程级失效（P1 修复）
                AUTO_GATE.release()
                return new_view
        except Exception as e:
            from agent.context_assembler import compaction as _comp
            _comp.AUTO_GATE.release()  # 压实失败解除闩锁，避免永久不再自动触发
            logger.warning(f"[Context] Assembly-exit compaction failed, using un-compacted view: {e}")
        return view

    def get_fold_dashboard_line(self) -> str:
        """动态块使用率仪表盘行（spec §5）：读最近一次组装缓存，无缓存返回 ""。

        格式：[上下文使用率 {u}% · 强制压缩线 {t}% · 可折叠输出 {n} 条（合计 {p}%）]
        - m==n 全有快照 →（合计 {p}%）；m<n 含 NULL 旧数据 →（其中 {m} 条合计 {p}%）
        - n==0 或 fold_columns_available() False（迁移失败降级）→ 省略可折叠段，
          只留使用率+压缩线——不误导 LLM 调必报错的工具（R2-B P3）
        """
        stats = self._fold_stats
        if not stats:
            return ""
        usage = stats.get("usage") or 0.0
        try:
            from agent.context_assembler.compaction import trigger_ratio
            t = trigger_ratio()
        except Exception:
            t = TRIGGER_RATIO_FALLBACK
        line = f"[上下文使用率 {usage * 100:.1f}% · 强制压缩线 {t * 100:g}%"
        n = stats.get("n")
        if n:
            m, p = stats["m"], stats["p"]
            if m == n:
                line += f" · 可折叠输出 {n} 条（合计 {p:g}%）"
            elif m > 0:
                line += f" · 可折叠输出 {n} 条（其中 {m} 条合计 {p:g}%）"
            else:
                line += f" · 可折叠输出 {n} 条（无占比快照）"
        return line + "]"


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

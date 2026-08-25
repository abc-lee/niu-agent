"""实体标签反查（Task 4）——按块时间范围从知识图谱取当天提及实体。

通道：会话实体展开（niu.md「知识图谱时间链」同源机制）——每个日期若有
会话实体 `YYYY-MM-DD会话`，其深度 1 邻居即当天被提炼挂载的实体。
纯机械查表、零 LLM；LightRAG 不可用/图读失败/无会话实体一律返回 []，
绝不阻塞组装主路径（spec §3.2「实体标签来源=纯查表」+ D7 确定性优先）。

注：GraphML 节点的 created_at 是入库时间而非对话时间，按其过滤块时间
范围会系统性漏采（提炼晚于对话）——故采用会话实体展开而非时间属性过滤。
"""

from __future__ import annotations

import re

from loguru import logger

MAX_TAGS_PER_BLOCK = 3   # spec §3.3：实体标签 ≤3 个
_MAX_SPAN_DAYS = 31      # 时间跨度保护：异常长块最多回溯 31 天
_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}会话$")
_ROOT_NODES = {"niu"}


def _dates_covered(time_start: str, time_end: str) -> list[str]:
    """ISO 时间戳区间覆盖的日期列表（YYYY-MM-DD，含端点）。解析失败返回空。"""
    from datetime import date, timedelta

    def _parse(ts: str):
        try:
            return date.fromisoformat(ts[:10])
        except (ValueError, TypeError, IndexError):
            return None

    d0, d1 = _parse(time_start), _parse(time_end)
    if d0 is None or d1 is None:
        return []
    if d1 < d0:
        d0, d1 = d1, d0
    days: list[str] = []
    cur = d0
    while cur <= d1 and len(days) < _MAX_SPAN_DAYS:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _graph_snapshot():
    """在图读锁内拷贝 NetworkX 快照（与 lightrag_adapter 直读同款模式）。

    失败返回 None——调用方降级为空标签。
    """
    try:
        from niu_api.internal.lightrag_manager import get_lightrag, graph_read_lock

        rag = get_lightrag()
        if rag is None:
            return None
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return None
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None:
            return None
        with graph_read_lock():
            return nx_graph.copy()
    except Exception as e:
        logger.debug(f"[EntityTags] graph snapshot unavailable: {e}")
        return None



def tags_for_range(snapshot, time_start: str, time_end: str) -> list[str]:
    """单个块时间范围内的实体标签（≤3 个）。snapshot 为 None 返回 []。

    排序：边权重降序 → 名称升序（确定性输出）；排除其他会话实体与根节点。
    """
    if snapshot is None:
        return []
    candidates: dict[str, float] = {}
    for day in _dates_covered(time_start, time_end):
        session_entity = f"{day}会话"
        try:
            if not snapshot.has_node(session_entity):
                continue
            # 邻接视图：{neighbor: {edge_key: attrs}}（Graph/MultiGraph 通用）
            edge_data = snapshot[session_entity]
        except Exception as e:
            logger.debug(f"[EntityTags] neighbor read failed for {session_entity}: {e}")
            continue
        for neighbor, data in edge_data.items():
            if neighbor in _ROOT_NODES or _SESSION_RE.match(neighbor or ""):
                continue
            w = 1.0
            if isinstance(data, dict):
                try:
                    w = float(data.get("weight", 1.0))
                except (TypeError, ValueError):
                    w = 1.0
            if w > candidates.get(neighbor, -1.0):
                candidates[neighbor] = w
    ordered = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ordered[:MAX_TAGS_PER_BLOCK]]


def collect_tags(time_ranges: list[tuple[str, str]]) -> list[list[str]]:
    """批量反查：一次快照服务全部新块。返回与入参等长的标签列表，永不抛出。"""
    if not time_ranges:
        return []
    try:
        snapshot = _graph_snapshot()
        return [tags_for_range(snapshot, t0, t1) for t0, t1 in time_ranges]
    except Exception as e:
        logger.debug(f"[EntityTags] collect failed, degrading to empty tags: {e}")
        return [[] for _ in time_ranges]

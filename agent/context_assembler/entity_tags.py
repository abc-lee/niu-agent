"""实体标签反查（Task 4）——按块时间范围从知识图谱取当天提及实体。

通道：会话实体展开（niu.md「知识图谱时间链」同源机制）——每个日期若有
会话实体 `YYYY-MM-DD会话`，其深度 1 邻居即当天被提炼挂载的实体。
时间链候选池 + 语义排序（spec 2026-09-05-entity-tags-semantic §3.2）：
提供 first_users 时在候选池内按「块首问向量·实体向量」余弦 top3，同日相邻块
标签可区分；零 LLM 保留（embedding 本地单例）。first_users=None 走纯时间链
查表（现行行为逐字节保持）。LightRAG 不可用/图读失败→空标签，语义段任何
失败→时间链权重 top3，绝不阻塞组装主路径（spec §3.4 降级链）。

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


def _candidate_pool(snapshot, time_start: str, time_end: str) -> dict[str, float]:
    """时间链候选池 {实体名: 边权重}（与 tags_for_range 同源遍历，不做 top3 截断）。

    仅语义段消费；snapshot 为 None 返回空池。
    """
    if snapshot is None:
        return {}
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
    return candidates


def _semantic_tags(snapshot, time_ranges: list[tuple[str, str]],
                   first_users: list[str]) -> list[list[str]]:
    """语义段（spec §3.2/§3.4）：时间链候选池内按「块首问向量·实体向量」
    相似度降序 top3；命中 <3 时按时间链权重序剔除已选实体补齐至 3。

    任何异常/不可用（encode 抛出/维度失配/vdb 失败/call_async 超时/其他）
    → 全批时间链权重 top3，绝不向外抛出。
    """
    def _fallback() -> list[list[str]]:
        return [tags_for_range(snapshot, t0, t1) for t0, t1 in time_ranges]

    try:
        # 首问空/全空白块排除出批量（该块用时间链）；全部过滤后跳过 encode
        valid = [(i, fu) for i, fu in enumerate(first_users)
                 if isinstance(fu, str) and fu.strip()]
        if not valid:
            return _fallback()

        # is_ready 门控：False → 语义段放弃（不触发加载）；TOCTOU 竞态残窗见 spec §3.3
        from niu_api.internal.embedding import batch_encode, is_ready

        if not is_ready():
            return _fallback()
        vectors = batch_encode([fu for _, fu in valid])

        # name→vector 映射在 lightrag loop 协程内构建（与写方同线程串行，天然原子）；
        # 读 data/matrix 期间不插入 await；data/matrix 长度不一致 → 防御性放弃语义段
        from niu_api.internal.lightrag_manager import call_async, get_lightrag

        async def _get_vdb_snapshot() -> dict[str, object] | None:
            rag = get_lightrag()
            if rag is None:
                return None
            vdb = getattr(rag, "entities_vdb", None)
            if vdb is None:
                return None
            client = await vdb._get_client()
            storage = client._NanoVectorDB__storage or {}
            data = storage.get("data") or []
            matrix = storage.get("matrix")
            if matrix is None or len(data) != len(matrix):
                return None
            mapping: dict[str, object] = {}
            for i, row in enumerate(data):
                name = row.get("entity_name") if isinstance(row, dict) else None
                if name and name not in mapping:
                    mapping[name] = matrix[i]
            return mapping or None

        ent_vecs = call_async(_get_vdb_snapshot(), timeout=5)
        if not ent_vecs:
            return _fallback()

        import numpy as np

        qmat = np.asarray(vectors, dtype=np.float32)
        q_by_block = {i: qmat[k] for k, (i, _) in enumerate(valid)}
        ent_dim = len(next(iter(ent_vecs.values())))
        results: list[list[str]] = []
        for i, (t0, t1) in enumerate(time_ranges):
            pool = _candidate_pool(snapshot, t0, t1)
            chain_top = [n for n, _ in sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))]
            qv = q_by_block.get(i)
            if qv is None or not pool:
                results.append(chain_top[:MAX_TAGS_PER_BLOCK])
                continue
            # dot 前维度检查：首问向量维度 ≠ 实体向量维度 → 全批时间链
            if qv.shape[0] != ent_dim:
                return _fallback()
            scored = []
            for name in pool:
                ev = ent_vecs.get(name)
                if ev is None:
                    continue  # 池内实体缺 vdb 向量 → 跳过
                scored.append((float(np.dot(qv, ev)), name))
            scored.sort(key=lambda t: (-t[0], t[1]))
            chosen = [name for _, name in scored[:MAX_TAGS_PER_BLOCK]]
            if len(chosen) < MAX_TAGS_PER_BLOCK:
                picked = set(chosen)
                for name in chain_top:  # 时间链权重序补齐至 3，剔除已选实体防重复
                    if len(chosen) >= MAX_TAGS_PER_BLOCK:
                        break
                    if name not in picked:
                        chosen.append(name)
            results.append(chosen)
        return results
    except Exception as e:
        logger.debug(f"[EntityTags] semantic segment failed, degrading to time-chain tags: {e}")
        return _fallback()


def collect_tags(time_ranges: list[tuple[str, str]],
                 first_users: list[str] | None = None) -> list[list[str]]:
    """批量反查：一次快照服务全部新块。返回与入参等长的标签列表，永不抛出。

    first_users=None → 纯时间链路径（现行行为逐字节保持）；提供且与 time_ranges
    等长 → 语义段（时间链候选池 + 首问向量排序，见 _semantic_tags）；长度不匹配
    → logger.warning + 全批时间链 top3。
    """
    if not time_ranges:
        return []
    try:
        snapshot = _graph_snapshot()
        if snapshot is None:
            return [[] for _ in time_ranges]
        if first_users is None or len(first_users) != len(time_ranges):
            if first_users is not None and len(first_users) != len(time_ranges):
                logger.warning(
                    f"[EntityTags] first_users length {len(first_users)} "
                    f"!= time_ranges length {len(time_ranges)}, "
                    "degrading to time-chain tags"
                )
            return [tags_for_range(snapshot, t0, t1) for t0, t1 in time_ranges]
        return _semantic_tags(snapshot, time_ranges, first_users)
    except Exception as e:
        logger.debug(f"[EntityTags] collect failed, degrading to empty tags: {e}")
        return [[] for _ in time_ranges]

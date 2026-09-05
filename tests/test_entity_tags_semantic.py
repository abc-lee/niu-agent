"""实体标签语义化行为锁（spec 2026-09-05-entity-tags-semantic §3.4/§4 T2）。

覆盖：
- 语义排序正确性（相似度降序 + 名称升序 tie-break，FakeEmbedModel 归一化向量）
- 补齐去重混合用例（语义命中 <3 → 时间链权重序剔除已选补齐；候选池跨天重复去重）
- §3.4 降级每级独立用例（is_ready False / encode 异常 / 维度失配 / call_async
  超时 / data-matrix 长度不一致 / first_users 长度不匹配 / 单块空首问 / 全空首问 /
  候选池空 / 图快照 None / 语义段 catch-all）→ 不抛出、落点正确
- first_users=None 等价性锁（与逐块 tags_for_range 输出一致）
- archive 接线传参断言（first_users 非空且与 time_ranges 等长）

全部 mock/临时 DB：FakeEmbedModel 替代真实 bge 模型，手造 vdb storage
（_NanoVectorDB__storage {data, matrix}），不加载 ~400MB 模型、不碰真实
~/.niu/lightrag_storage。entity_tags 函数内局部 import → patch 目标为
niu_api.internal.embedding / niu_api.internal.lightrag_manager 模块属性级。
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from loguru import logger

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from agent.context_assembler import compaction, entity_tags  # noqa: E402
from niu_api.internal import embedding as _emb  # noqa: E402
from niu_api.internal import lightrag_manager as _lrm  # noqa: E402

# =============================================================================
# Fake 基础设施
# =============================================================================

def _norm(v) -> np.ndarray:
    """L2 归一化（测试向量统一口径，与生产 matrix 已归一化一致）。"""
    a = np.asarray(v, dtype=np.float32)
    return a / np.linalg.norm(a)


class FakeEmbedModel:
    """假 embedding 模型：固定 首问→向量 表（L2 归一化），记录每次 encode 入参。

    未知文本返回零向量（防御，正常用例不应触达）。
    """

    def __init__(self, table: dict[str, list[float]]):
        self.table = {t: _norm(v) for t, v in table.items()}
        self.dim = len(next(iter(table.values())))
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]):
        self.calls.append(list(texts))
        return [
            (self.table[t] if t in self.table else np.zeros(self.dim, np.float32)).tolist()
            for t in texts
        ]


class FakeGraph:
    """最小邻接图（与 test_read_block_tool.TestEntityTags 同款接口）。"""

    def __init__(self, nodes, edges):
        self._nodes, self._edges = nodes, edges

    def has_node(self, n):
        return n in self._nodes

    def __getitem__(self, n):  # 邻接视图（与 networkx 一致）
        return self._edges.get(n)

    def copy(self):  # _graph_snapshot 走 nx_graph.copy()
        return FakeGraph(self._nodes, self._edges)


class _FakeVDBClient:
    def __init__(self, storage):
        self._NanoVectorDB__storage = storage


class _FakeEntitiesVDB:
    def __init__(self, client):
        self._client = client

    async def _get_client(self):
        return self._client


class _FakeGraphObj:
    def __init__(self, graph):
        self._graph = graph


class _FakeRag:
    def __init__(self, graph, storage):
        self.chunk_entity_relation_graph = _FakeGraphObj(graph)
        self.entities_vdb = _FakeEntitiesVDB(_FakeVDBClient(storage))


def _storage(rows: list[tuple[str, np.ndarray]]) -> dict:
    """手造 NanoVectorDB storage：data 行含 entity_name，matrix 逐行对齐。"""
    return {
        "data": [{"entity_name": name} for name, _ in rows],
        "matrix": np.stack([v for _, v in rows]).astype(np.float32),
    }


def _run_coro(coro, timeout: int = 120):
    """call_async 替身：新事件循环内跑完协程（零 await 的 _get_vdb_snapshot 安全）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout))
    finally:
        loop.close()


# ---- 标准图：2026-09-04 会话，4 实体 + 应排除的根/会话节点 ----
# 时间链 top3（权重降序）= [Delta(4), Charlie(3), Bravo(2)]
_STD_GRAPH = FakeGraph(
    nodes={"2026-09-04会话"},
    edges={
        "2026-09-04会话": {
            "Alpha": {"weight": 1.0},
            "Bravo": {"weight": 2.0},
            "Charlie": {"weight": 3.0},
            "Delta": {"weight": 4.0},
            "niu": {"weight": 9.0},                # 根节点应被排除
            "2026-09-03会话": {"weight": 9.0},      # 会话实体应被排除
        },
    },
)
_STD_VECTORS = {
    "Alpha": _norm([1, 0, 0]),
    "Bravo": _norm([0, 1, 0]),
    "Charlie": _norm([0, 0, 1]),
    "Delta": _norm([1, 1, 1]),
}
_STD_STORAGE = _storage(list(_STD_VECTORS.items()))
_CHAIN_TOP3 = ["Delta", "Charlie", "Bravo"]

# q=[3,2,1] 归一化后 dot：Delta≈0.926 > Alpha≈0.802 > Bravo≈0.535 > Charlie≈0.267
_Q_DIET = [3.0, 2.0, 1.0]
_SEMANTIC_TOP3 = ["Delta", "Alpha", "Bravo"]

# ---- tie-break 图：Zulu/Alpha 对 q=[1,1,0] dot 严格相等 ----
# 图邻接序与权重序都是 Zulu 在前 → 只有名称升序 tie-break 才产出 [Alpha, Zulu, ...]
_TIE_GRAPH = FakeGraph(
    nodes={"2026-09-04会话"},
    edges={
        "2026-09-04会话": {
            "Zulu": {"weight": 5.0},
            "Alpha": {"weight": 4.0},
            "Mid": {"weight": 3.0},
        },
    },
)
_TIE_VECTORS = {
    "Zulu": _norm([1, 0, 0]),
    "Alpha": _norm([0, 1, 0]),
    "Mid": _norm([0, 0, 1]),
}


def _install(monkeypatch, graph, storage=None, *, model=None, ready=True,
             call_async=None):
    """安装 fake rag + embedding/call_async patch。返回 model（可为 None）。"""
    monkeypatch.setattr(_lrm, "get_lightrag", lambda: _FakeRag(graph, storage))
    if call_async is not None:
        monkeypatch.setattr(_lrm, "call_async", call_async)
    else:
        monkeypatch.setattr(_lrm, "call_async", _run_coro)
    if model is not None:
        monkeypatch.setattr(_emb, "is_ready", lambda: ready)
        monkeypatch.setattr(_emb, "batch_encode", model.encode)
    return model


def _one_range():
    return [("2026-09-04T08:00:00", "2026-09-04T12:00:00")]


# =============================================================================
# ① 语义排序正确性
# =============================================================================

class TestSemanticOrdering:
    def test_top3_by_similarity_desc(self, monkeypatch):
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model)
        result = entity_tags.collect_tags(_one_range(), ["q-diet"])
        # 语义序 [Delta, Alpha, Bravo] ≠ 时间链序 [Delta, Charlie, Bravo]——排序确实生效
        assert result == [_SEMANTIC_TOP3]
        assert result[0] != _CHAIN_TOP3

    def test_tie_break_name_ascending(self, monkeypatch):
        model = FakeEmbedModel({"q-tie": [1.0, 1.0, 0.0]})
        _install(monkeypatch, _TIE_GRAPH, _storage(list(_TIE_VECTORS.items())),
                 model=model)
        result = entity_tags.collect_tags(_one_range(), ["q-tie"])
        # Zulu/Alpha dot 严格相等 → 名称升序；Mid(0.0) 垫底
        assert result == [["Alpha", "Zulu", "Mid"]]


# =============================================================================
# ② 补齐去重混合用例
# =============================================================================

class TestBackfillDedup:
    def test_backfill_dedups_selected_and_pool_duplicates(self, monkeypatch):
        """跨天候选池重复（A 两天都挂）+ 语义命中 2 → 时间链序剔除已选补 1。"""
        graph = FakeGraph(
            nodes={"2026-09-03会话", "2026-09-04会话"},
            edges={
                "2026-09-03会话": {"A": {"weight": 1.0}},   # 重复候选（低权重）
                "2026-09-04会话": {
                    "A": {"weight": 4.0},                   # 同名取 max=4.0
                    "B": {"weight": 3.0},
                    "C": {"weight": 2.0},
                    "D": {"weight": 1.0},
                },
            },
        )
        # vdb 只有 A/B 向量 → C/D 缺向量被跳过，语义命中 2 个（A/B dot 相等→名称序）
        storage = _storage([("A", _norm([1, 0, 0])), ("B", _norm([0, 1, 0]))])
        model = FakeEmbedModel({"q-back": [1.0, 1.0, 0.0]})
        _install(monkeypatch, graph, storage, model=model)

        ranges = [("2026-09-03T08:00:00", "2026-09-04T12:00:00")]
        result = entity_tags.collect_tags(ranges, ["q-back"])

        assert result == [["A", "B", "C"]]   # C=时间链序第 3（D 被挤出 top3）
        assert len(set(result[0])) == 3      # 补齐剔除已选 → 无重复


# =============================================================================
# ③ §3.4 降级每级独立用例
# =============================================================================

class TestDegradation:
    def test_is_ready_false_skips_encode(self, monkeypatch):
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model, ready=False)
        result = entity_tags.collect_tags(_one_range() * 2, ["q-diet", "q-diet"])
        assert result == [_CHAIN_TOP3, _CHAIN_TOP3]   # 全批时间链
        assert model.calls == []                      # 不触发 encode（不触发加载）

    def test_encode_exception_falls_back(self, monkeypatch):
        def _boom(texts):
            raise RuntimeError("model load blocked")
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE)
        monkeypatch.setattr(_emb, "is_ready", lambda: True)
        monkeypatch.setattr(_emb, "batch_encode", _boom)
        result = entity_tags.collect_tags(_one_range() * 2, ["q-diet", "q-diet"])
        assert result == [_CHAIN_TOP3, _CHAIN_TOP3]   # 全批时间链，不抛出

    def test_dim_mismatch_falls_back(self, monkeypatch):
        """首问向量 dim=4 ≠ matrix dim=3 → 全批时间链（换模型未重建库形态）。"""
        model = FakeEmbedModel({"q-diet": [1.0, 0.5, 0.25, 0.1]})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model)
        result = entity_tags.collect_tags(_one_range(), ["q-diet"])
        assert result == [_CHAIN_TOP3]

    def test_call_async_timeout_falls_back(self, monkeypatch):
        captured: dict[str, object] = {}

        def _timeout(coro, timeout=120):
            captured["timeout"] = timeout
            coro.close()  # 真实 call_async 超时后关闭协程，避免 unawaited 告警
            raise TimeoutError("call_async timeout")
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model,
                 call_async=_timeout)
        result = entity_tags.collect_tags(_one_range(), ["q-diet"])
        assert result == [_CHAIN_TOP3]
        # 生产调用点必须显式 timeout=5（真实默认 120s，冻结主事件循环上界）
        assert captured.get("timeout") == 5

    def test_data_matrix_len_mismatch_falls_back(self, monkeypatch):
        """防御行：data 4 行 vs matrix 3 行 → 全批时间链。"""
        storage = {"data": _STD_STORAGE["data"], "matrix": _STD_STORAGE["matrix"][:3]}
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, storage, model=model)
        result = entity_tags.collect_tags(_one_range(), ["q-diet"])
        assert result == [_CHAIN_TOP3]

    def test_first_users_short_warns_and_falls_back(self, monkeypatch):
        """first_users 少一条 → logger.warning + 全批时间链。"""
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model)
        records: list[str] = []
        sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
        try:
            result = entity_tags.collect_tags(_one_range() * 2, ["q-diet"])
        finally:
            logger.remove(sink_id)
        assert result == [_CHAIN_TOP3, _CHAIN_TOP3]
        assert any("first_users length" in r for r in records)

    def test_empty_first_user_single_block_chain_others_semantic(self, monkeypatch):
        """单块空首问 → 该块时间链，他块语义；空首问排除出批量。"""
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model)
        result = entity_tags.collect_tags(_one_range() * 2, ["q-diet", ""])
        assert result == [_SEMANTIC_TOP3, _CHAIN_TOP3]
        assert model.calls == [["q-diet"]]   # 空首问未进批量

    def test_all_empty_first_users_skip_encode(self, monkeypatch):
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE, model=model)
        result = entity_tags.collect_tags(_one_range() * 2, ["", "   "])
        assert result == [_CHAIN_TOP3, _CHAIN_TOP3]
        assert model.calls == []            # 全空 → 不触发 encode

    def test_empty_candidate_pool_empty_tags(self, monkeypatch):
        """块日期无会话节点 → 候选池空 → 空标签（既有语义）。"""
        graph = FakeGraph(
            nodes={"2026-08-01会话"},
            edges={"2026-08-01会话": {"Alpha": {"weight": 1.0}}},
        )
        model = FakeEmbedModel({"q-diet": _Q_DIET})
        _install(monkeypatch, graph, _STD_STORAGE, model=model)
        assert entity_tags.collect_tags(_one_range(), ["q-diet"]) == [[]]

    def test_snapshot_none_empty_tags(self, monkeypatch):
        """图快照 None（get_lightrag None/图读失败）→ 空标签（既有）。"""
        with patch.object(entity_tags, "_graph_snapshot", return_value=None):
            result = entity_tags.collect_tags(
                _one_range() * 2, ["q-diet", "q-diet"])
        assert result == [[], []]

    def test_semantic_catchall_falls_back(self, monkeypatch):
        """语义段未预期异常（encode 返回非数值）→ 全批时间链，不抛出。"""
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE)
        monkeypatch.setattr(_emb, "is_ready", lambda: True)
        monkeypatch.setattr(_emb, "batch_encode", lambda texts: [None])
        result = entity_tags.collect_tags(_one_range(), ["q-diet"])
        assert result == [_CHAIN_TOP3]


# =============================================================================
# ④ first_users=None 等价性锁（AC4）
# =============================================================================

class TestFirstUsersNoneEquivalence:
    def test_none_matches_per_block_tags_for_range(self, monkeypatch):
        """first_users=None → 与逐块 tags_for_range 输出一致（现行行为保持）。"""
        _install(monkeypatch, _STD_GRAPH, _STD_STORAGE)
        ranges = [
            ("2026-09-03T10:00:00", "2026-09-04T11:00:00"),  # 跨天块
            ("2026-09-04T08:00:00", "2026-09-04T12:00:00"),
        ]
        snapshot = entity_tags._graph_snapshot()
        expected = [entity_tags.tags_for_range(snapshot, t0, t1) for t0, t1 in ranges]
        assert expected[0]  # 非平凡：锁有判别力（不是全空对全空）

        assert entity_tags.collect_tags(ranges) == expected
        assert entity_tags.collect_tags(ranges, None) == expected


# =============================================================================
# ⑤ archive 接线传参断言
# =============================================================================

class TestArchiveWiring:
    def test_archive_passes_first_users(self, tmp_path, monkeypatch):
        """archive_excluded_units 双参调用：first_users 非空且与 time_ranges 等长。"""
        captured = {}

        def _spy(ranges, first_users=None):
            captured["ranges"] = list(ranges)
            captured["first_users"] = None if first_users is None else list(first_users)
            return [[] for _ in ranges]

        monkeypatch.setattr(entity_tags, "collect_tags", _spy)

        def msg(role, content, mid, rowid, created_at):
            return SimpleNamespace(
                id=mid, rowid=rowid, role=role, content=content,
                tool_calls=None, tool_call_id=None, created_at=created_at,
            )

        messages = [
            msg("user", "帮我查一下醋溜白菜的做法", "u1", 1, "2026-09-04T08:00:00"),
            msg("assistant", "a1", "a1", 2, "2026-09-04T08:01:00"),
        ]
        added = compaction.archive_excluded_units(
            messages, [(0, 1)], 99, tmp_path / "b.db")
        assert added == 1

        fu = captured["first_users"]
        assert isinstance(fu, list) and len(fu) == 1
        assert len(fu) == len(captured["ranges"])      # 与 time_ranges 等长
        assert fu[0] == "帮我查一下醋溜白菜的做法"       # 非空、取自块首问


# =============================================================================
# ⑥ tags_for_range / _candidate_pool parity 契约（FinalReview P3-3）
# =============================================================================

class TestCandidatePoolParity:
    """双副本同源遍历一致性：时间链 top3 == 候选池权重序前 3——防未来一处
    演进另一处漏改，致纯时间链路径与语义候选池静默分叉。"""

    def test_parity_more_than_three_entities(self):
        """>3 实体（4 + 应排除根/会话节点）：top3 截断路径一致。"""
        t0, t1 = _one_range()[0]
        snapshot = _STD_GRAPH.copy()   # _graph_snapshot 形态（nx_graph.copy()）
        pool = entity_tags._candidate_pool(snapshot, t0, t1)
        assert len(pool) == 4          # 非平凡：根/会话节点被排除，截断真实生效
        top3 = [n for n, _ in sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
        assert top3 == entity_tags.tags_for_range(snapshot, t0, t1)
        assert top3 == _CHAIN_TOP3

    def test_parity_three_or_fewer_entities(self):
        """≤3 实体（3）：全池权重序 == tags（无截断）。"""
        t0, t1 = _one_range()[0]
        snapshot = _TIE_GRAPH.copy()
        pool = entity_tags._candidate_pool(snapshot, t0, t1)
        assert len(pool) == 3
        top3 = [n for n, _ in sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
        assert top3 == entity_tags.tags_for_range(snapshot, t0, t1)
        assert top3 == ["Zulu", "Alpha", "Mid"]   # 权重降序（5>4>3）

    def test_parity_no_session_node_empty(self):
        """块日期无会话节点 → 空池 {} 与 [] 一致。"""
        graph = FakeGraph(
            nodes={"2026-08-01会话"},
            edges={"2026-08-01会话": {"Alpha": {"weight": 1.0}}},
        )
        t0, t1 = _one_range()[0]   # 2026-09-04，图里只有 2026-08-01 会话
        snapshot = graph.copy()
        assert entity_tags._candidate_pool(snapshot, t0, t1) == {}
        assert entity_tags.tags_for_range(snapshot, t0, t1) == []

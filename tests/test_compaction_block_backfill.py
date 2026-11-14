"""压实后归档块的实体标签补齐（提炼后置的补偿）。"""
from contextlib import contextmanager
from unittest.mock import patch

import agent.context_assembler  # noqa: F401,E402  导入序守卫（同其它 compaction 测试）

from agent.context_assembler.blocks import PointerBlock, load_all, upsert_blocks
from agent.context_assembler.compaction import backfill_block_entities


def _blk(i, t0, t1, entities=None):
    return PointerBlock(
        id=i, start_msg_id=f"a{i}", end_msg_id=f"b{i}",
        start_rowid=i * 10, end_rowid=i * 10 + 5, count=6,
        time_start=t0, time_end=t1, entities=list(entities or []),
    )


class _PoolGraph:
    """图快照替身：任一「YYYY-MM-DD会话」节点都挂同一实体 → 候选池恒非空。

    backfill 在调 collect_tags 前用 tags_for_range 做「候选池空」剪枝，剪枝空则
    整块跳过；这些用例锁的是传参/计数/轮询，故快照必须给出非空池。
    """

    def has_node(self, node):
        return str(node).endswith("会话")

    def __getitem__(self, node):
        return {"A": {"weight": 1.0}}


@contextmanager
def _graph(tags):
    """模拟图可用 + 标签结果（快照/向量只取一次，collect_tags 逐块返回）。"""
    with patch("agent.context_assembler.entity_tags._graph_snapshot",
               return_value=_PoolGraph()), \
         patch("agent.context_assembler.entity_tags._vdb_snapshot", return_value={}), \
         patch("agent.context_assembler.entity_tags.collect_tags", return_value=tags):
        yield


def test_backfill_fills_only_empty_entities(tmp_path):
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(1, "2026-11-01T10:00:00", "2026-11-01T11:00:00"),
                   _blk(2, "2026-11-02T10:00:00", "2026-11-02T11:00:00", ["已有"])], db)
    with _graph([["A", "B", "C"]]):
        n = backfill_block_entities(db)
    assert n == 1
    got = {b.id: b.entities for b in load_all(db)}
    assert got[1] == ["A", "B", "C"]
    assert got[2] == ["已有"]          # 非空不覆盖


def test_backfill_noop_when_tags_empty(tmp_path):
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(1, "2026-11-01T10:00:00", "2026-11-01T11:00:00")], db)
    with _graph([[]]):
        assert backfill_block_entities(db) == 0
    assert load_all(db)[0].entities == []


def test_backfill_stops_when_sleep_ends(tmp_path):
    """用户唤醒 → 每补一块轮询一次 should_continue，立即停止；已补的保留。"""
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(i, f"2026-11-0{i}T10:00:00", f"2026-11-0{i}T11:00:00")
                   for i in (1, 2, 3)], db)
    calls = {"n": 0}

    def cont():
        calls["n"] += 1
        return calls["n"] <= 2          # 第 3 次轮询返回 False

    with _graph([["A"]]):
        n = backfill_block_entities(db, should_continue=cont)

    assert n == 2
    got = {b.id: b.entities for b in load_all(db)}
    assert got[1] == ["A"] and got[2] == ["A"]
    assert got[3] == []                  # 未处理，下次睡眠继续


def test_backfill_fetches_snapshot_once(tmp_path):
    """共享快照是唯一性能承诺：整轮只取一次图快照与实体向量。"""
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(i, f"2026-11-0{i}T10:00:00", f"2026-11-0{i}T11:00:00")
                   for i in (1, 2, 3)], db)
    with patch("agent.context_assembler.entity_tags._graph_snapshot",
               return_value=_PoolGraph()) as m_snap, \
         patch("agent.context_assembler.entity_tags._vdb_snapshot", return_value={}) as m_vdb, \
         patch("agent.context_assembler.entity_tags.collect_tags", return_value=[["A"]]) as m_collect:
        backfill_block_entities(db)
    assert m_snap.call_count == 1
    assert m_vdb.call_count == 1
    assert m_collect.call_args.kwargs["snapshot"] is not None
    assert m_collect.call_args.kwargs["ent_vecs"] == {}


def test_backfill_returns_zero_when_graph_unavailable(tmp_path):
    """图快照取不到 → 直接 0，不逐块重取。"""
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(1, "2026-11-01T10:00:00", "2026-11-01T11:00:00")], db)
    with patch("agent.context_assembler.entity_tags._graph_snapshot", return_value=None), \
         patch("agent.context_assembler.entity_tags.collect_tags") as m_collect:
        assert backfill_block_entities(db) == 0
        m_collect.assert_not_called()


def test_backfill_never_raises(tmp_path):
    """向量快照不可用（返回 None，不抛）→ 只取一次并传 {}；collect_tags 异常按块跳过。"""
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(1, "2026-11-01T10:00:00", "2026-11-01T11:00:00")], db)
    with patch("agent.context_assembler.entity_tags._graph_snapshot",
               return_value=_PoolGraph()), \
         patch("agent.context_assembler.entity_tags._vdb_snapshot", return_value=None) as m_vdb, \
         patch("agent.context_assembler.entity_tags.collect_tags", side_effect=RuntimeError("graph down")) as m_collect:
        assert backfill_block_entities(db) == 0
    assert m_vdb.call_count == 1
    assert m_collect.call_args.kwargs["ent_vecs"] == {}


def test_backfill_skips_block_with_empty_candidate_pool(tmp_path):
    """候选池空（块日期无会话节点）→ 剪枝直接跳过，不调 collect_tags（不白跑 encode）。"""
    db = tmp_path / "blocks.db"
    upsert_blocks([_blk(1, "2026-11-01T10:00:00", "2026-11-01T11:00:00")], db)

    class _NoSessionGraph:
        def has_node(self, node):
            return False

        def __getitem__(self, node):
            raise KeyError(node)

    with patch("agent.context_assembler.entity_tags._graph_snapshot",
               return_value=_NoSessionGraph()), \
         patch("agent.context_assembler.entity_tags._vdb_snapshot", return_value={}), \
         patch("agent.context_assembler.entity_tags.collect_tags") as m_collect:
        assert backfill_block_entities(db) == 0
    m_collect.assert_not_called()
    assert load_all(db)[0].entities == []

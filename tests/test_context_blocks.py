"""指针块存储层测试（agent/context_assembler/blocks.py）。

全部用临时目录 DB，隔离真实 ~/.niu。
"""

import sqlite3
from threading import Thread
from typing import Any

import pytest

from agent.context_assembler import blocks as blocks_module
from agent.context_assembler.blocks import (
    BUSY_TIMEOUT_MS,
    PointerBlock,
    delete_all,
    load_all,
    load_by_ids,
    query_by_msg_id,
    rowid_range_query,
    upsert_blocks,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "context_blocks.db"


def make_block(bid: int, start_rowid: int, end_rowid: int, **over: Any) -> PointerBlock:
    kwargs: dict[str, Any] = {
        "id": bid,
        "start_msg_id": f"msg-{bid}-start",
        "end_msg_id": f"msg-{bid}-end",
        "start_rowid": start_rowid,
        "end_rowid": end_rowid,
        "count": end_rowid - start_rowid + 1,
        "time_start": f"2026-08-{10 + bid}T10:00:00",
        "time_end": f"2026-08-{10 + bid}T11:00:00",
        "entities": [f"实体{bid}"],
        "first_user": f"第{bid}块的首问内容",
    }
    kwargs.update(over)
    return PointerBlock(**kwargs)


class TestRoundTrip:
    def test_upsert_then_load_all_field_fidelity(self, db_path):
        blocks = [
            make_block(1, 10, 20),
            make_block(2, 30, 40, summary="两行摘要", summary_state="done", session="s2"),
        ]
        assert upsert_blocks(blocks, db_path) == 2
        loaded = load_all(db_path)
        assert loaded == blocks  # dataclass 全字段相等

    def test_load_empty_db_returns_empty_list(self, db_path):
        assert load_all(db_path) == []

    def test_load_all_ordered_by_id(self, db_path):
        upsert_blocks([make_block(3, 5, 6), make_block(1, 1, 2), make_block(2, 3, 4)], db_path)
        assert [b.id for b in load_all(db_path)] == [1, 2, 3]

    def test_entities_json_roundtrip(self, db_path):
        upsert_blocks([make_block(1, 1, 2, entities=["咖啡机定时任务", "HN抓取"])], db_path)
        assert load_all(db_path)[0].entities == ["咖啡机定时任务", "HN抓取"]

    def test_first_user_clamped_to_40_chars(self, db_path):
        long_q = "超" * 100
        upsert_blocks([make_block(1, 1, 2, first_user=long_q)], db_path)
        loaded = load_all(db_path)[0]
        assert len(loaded.first_user) == 40


class TestUpsertSemantics:
    def test_same_id_is_replace_not_duplicate(self, db_path):
        upsert_blocks([make_block(1, 1, 2)], db_path)
        updated = make_block(1, 1, 2, summary="已摘要", summary_state="done")
        upsert_blocks([updated], db_path)
        loaded = load_all(db_path)
        assert len(loaded) == 1
        assert loaded[0].summary == "已摘要"
        assert loaded[0].summary_state == "done"

    def test_table_creation_idempotent(self, db_path):
        upsert_blocks([make_block(1, 1, 2)], db_path)
        upsert_blocks([make_block(2, 3, 4)], db_path)
        assert [b.id for b in load_all(db_path)] == [1, 2]

    def test_empty_batch_noop(self, db_path):
        assert upsert_blocks([], db_path) == 0
        assert load_all(db_path) == []


class TestQueries:
    def test_load_by_ids_preserves_input_order_skips_missing(self, db_path):
        upsert_blocks([make_block(i, i * 10, i * 10 + 1) for i in range(1, 5)], db_path)
        got = load_by_ids([3, 99, 1], db_path)
        assert [b.id for b in got] == [3, 1]

    def test_load_by_ids_empty(self, db_path):
        assert load_by_ids([], db_path) == []

    def test_rowid_range_query_overlap_semantics(self, db_path):
        # 块1: rowid 10-20，块2: rowid 30-40
        upsert_blocks([make_block(1, 10, 20), make_block(2, 30, 40)], db_path)
        assert [b.id for b in rowid_range_query(15, 35, db_path)] == [1, 2]  # 双重叠
        assert [b.id for b in rowid_range_query(18, 22, db_path)] == [1]  # 半重叠
        assert rowid_range_query(21, 29, db_path) == []  # 夹缝
        assert rowid_range_query(50, 60, db_path) == []  # 区间之外
        assert rowid_range_query(5, 4, db_path) == []  # 非法区间

    def test_rowid_range_boundary_inclusive(self, db_path):
        upsert_blocks([make_block(1, 10, 20)], db_path)
        assert [b.id for b in rowid_range_query(10, 10, db_path)] == [1]
        assert [b.id for b in rowid_range_query(20, 25, db_path)] == [1]
        assert rowid_range_query(21, 21, db_path) == []
    def test_query_by_msg_id_hit(self, db_path):
        # 块1: msg-a..msg-m，块2: msg-n..msg-z（字典序闭区间）
        upsert_blocks(
            [
                make_block(1, 10, 20, start_msg_id="msg-a", end_msg_id="msg-m"),
                make_block(2, 30, 40, start_msg_id="msg-n", end_msg_id="msg-z"),
            ],
            db_path,
        )
        assert [b.id for b in query_by_msg_id("msg-c", db_path)] == [1]  # 命中块1
        assert [b.id for b in query_by_msg_id("msg-t", db_path)] == [2]  # 命中块2
        assert [b.id for b in query_by_msg_id("msg-a", db_path)] == [1]  # 左端点含
        assert [b.id for b in query_by_msg_id("msg-z", db_path)] == [2]  # 右端点含

    def test_query_by_msg_id_miss(self, db_path):
        upsert_blocks([make_block(1, 10, 20, start_msg_id="msg-b", end_msg_id="msg-y")], db_path)
        assert query_by_msg_id("msg-a", db_path) == []  # 区间之前
        assert query_by_msg_id("msg-z", db_path) == []  # 区间之后
        assert query_by_msg_id("", db_path) == []  # 空入参直接短路

    def test_query_by_msg_id_missing_db(self, tmp_path):
        assert query_by_msg_id("msg-x", tmp_path / "nonexistent.db") == []


class TestDeleteAll:
    def test_delete_all_keeps_table_usable(self, db_path):
        upsert_blocks([make_block(1, 1, 2), make_block(2, 3, 4)], db_path)
        assert delete_all(db_path) is True
        assert load_all(db_path) == []
        # 清空后可继续写入（表结构保留）
        upsert_blocks([make_block(9, 90, 91)], db_path)
        assert [b.id for b in load_all(db_path)] == [9]

    def test_delete_all_on_missing_db(self, tmp_path):
        assert delete_all(tmp_path / "nonexistent.db") is True


class TestCorruptionTolerance:
    @staticmethod
    def _capture_loguru():
        """blocks.py 用 loguru，pytest caplog 捕获不到——sink 捕获（项目既有模式）。"""
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        return messages, sink_id

    def test_corrupt_db_read_returns_empty_with_warning(self, db_path):
        from loguru import logger

        db_path.write_bytes(b"this is not a sqlite database at all")
        records, sink_id = self._capture_loguru()
        try:
            assert load_all(db_path) == []
            assert rowid_range_query(0, 999, db_path) == []
            assert load_by_ids([1], db_path) == []
        finally:
            logger.remove(sink_id)
        assert any("损坏" in r for r in records)

    def test_corrupt_db_write_returns_zero_no_raise(self, db_path):
        from loguru import logger

        db_path.write_bytes(b"garbage not a database")
        records, sink_id = self._capture_loguru()
        try:
            assert upsert_blocks([make_block(1, 1, 2)], db_path) == 0
            assert delete_all(db_path) is False
        finally:
            logger.remove(sink_id)
        assert any("失败" in r for r in records)

    def test_wal_mode_active(self, db_path):
        upsert_blocks([make_block(1, 1, 2)], db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode == "wal"

    def test_busy_timeout_set(self, db_path, monkeypatch):
        """_connect 必须显式执行 PRAGMA busy_timeout。

        只读新连接的 PRAGMA busy_timeout 读数是恒真断言：sqlite3.connect(timeout=5)
        已隐式把 busy timeout 置 5000ms。改为 spy sqlite3.connect 观察实参 +
        实际执行的语句，证明显式 PRAGMA 存在且参数来自 BUSY_TIMEOUT_MS。
        """
        assert BUSY_TIMEOUT_MS == 5000

        connect_kwargs: dict[str, Any] = {}
        executed_sql: list[str] = []
        real_connect = sqlite3.connect

        def spy_connect(path, *args, **kwargs):
            connect_kwargs.update(kwargs)
            real_conn = real_connect(path, *args, **kwargs)

            class RecordingConn:
                def execute(self, sql, *a, **kw):
                    executed_sql.append(sql)
                    return real_conn.execute(sql, *a, **kw)

                def __getattr__(self, name):
                    return getattr(real_conn, name)

            return RecordingConn()

        monkeypatch.setattr(blocks_module.sqlite3, "connect", spy_connect)
        upsert_blocks([make_block(1, 1, 2)], db_path)

        # connect(timeout=...) 参数传递 + 显式 PRAGMA busy_timeout 语句均已执行
        assert connect_kwargs.get("timeout") == BUSY_TIMEOUT_MS / 1000
        assert any(
            sql.replace(" ", "").startswith("PRAGMAbusy_timeout=5000") for sql in executed_sql
        )


class TestConcurrency:
    def test_parallel_writer_threads_all_blocks_landed(self, db_path):
        """多线程并发写（flock + busy_timeout 纪律）不丢块。"""
        n_threads, per_thread = 4, 10

        def worker(tid: int):
            for j in range(per_thread):
                upsert_blocks([make_block(tid * per_thread + j, 1, 2)], db_path)

        threads = [Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(load_all(db_path)) == n_threads * per_thread

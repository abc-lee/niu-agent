"""指针块一致性校验与整库重建测试（agent/context_assembler/integrity.py，Task 8）。

覆盖（计划 §Task 8 测试清单）：
1. 正常启动通过：空块库 / 一致块 → ok、零 issues、不触发重建
2. 漂移注入（DB 删行）→ 检测出 issues + 自动整库重建 → 复检一致
3. 损坏文件 → 删文件重建 → 复检一致
4. 检测失败 → ok=True + check_failed=True，不误判损坏、不触发重建
5. 区间重叠/倒置 → 检测 + 重建
6. /new 清理面：reset_derived_state 删块库 + 倍率复位 + 闸门解除 + 内存状态作废
7. 整库重建水位线语义：重建 = 全部单元按「留最近 keepRecentTurns 轮」归档

全部用临时目录 DB，隔离真实 ~/.niu；mock token 计数与保留轮数配置，
禁真实 LLM/LightRAG。
"""

import sqlite3
from pathlib import Path

import pytest

from agent.context_assembler import calibration, integrity, reset_derived_state
from agent.context_assembler.blocks import PointerBlock, load_all, upsert_blocks
from agent.context_assembler.calibration import DEFAULT_RATIO
from agent.context_assembler.compaction import AUTO_GATE
from agent.context_manager import ContextManager


# ---------------------------------------------------------------------------
# 测试基建
# ---------------------------------------------------------------------------

def _fake_count_tokens(messages):
    """确定性计数：每条消息 = len(content) + 8 结构开销。"""
    return sum(len(m.get("content", "")) + 8 for m in messages)


@pytest.fixture(autouse=True)
def _deterministic_tokens(monkeypatch):
    # staticmethod：实例访问/类访问两条路径都拿到裸函数（test_calibration.py 先例）
    monkeypatch.setattr(
        ContextManager, "count_tokens_simple", staticmethod(_fake_count_tokens)
    )
    # 保留轮数固定 3：整库重建语义不随真实用户配置漂移
    monkeypatch.setattr(
        "agent.context_assembler.compaction._read_keep_recent_turns", lambda: 3
    )


@pytest.fixture
def blocks_db(tmp_path):
    return tmp_path / "context_blocks.db"


@pytest.fixture
def messages_db(tmp_path):
    return tmp_path / "messages.db"


def _create_messages_db(path: Path, n_rows: int) -> None:
    """建 messages.db：n_rows 行 user/assistant 交替（每两行一个会话单元）。

    rowid 自动分配 1..n；msg id 为 m{rowid}；content 固定 10 字符（fake 计数 18）。
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT NOT NULL, "
        "content TEXT, created_at TEXT NOT NULL)"
    )
    for i in range(1, n_rows + 1):
        role = "user" if i % 2 == 1 else "assistant"
        conn.execute(
            "INSERT INTO messages (id, role, content, created_at) VALUES (?,?,?,?)",
            (f"m{i}", role, "x" * 10, f"2026-08-20T10:{i:02d}:00"),
        )
    conn.commit()
    conn.close()


def _delete_row(path: Path, rowid: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM messages WHERE rowid = ?", (rowid,))
    conn.commit()
    conn.close()


def _block(bid: int, r_start: int, r_end: int, count: int) -> PointerBlock:
    return PointerBlock(
        id=bid,
        start_msg_id=f"m{r_start}",
        end_msg_id=f"m{r_end}",
        start_rowid=r_start,
        end_rowid=r_end,
        count=count,
        time_start="2026-08-20T10:00:00",
        time_end="2026-08-20T10:01:00",
        entities=[],
        first_user="首问",
    )


# ---------------------------------------------------------------------------
# 1. 正常启动通过
# ---------------------------------------------------------------------------

class TestConsistentState:
    def test_no_blocks_db_and_messages_ok(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 4)
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result == {"ok": True, "issues": [], "repaired": False}

    def test_empty_blocks_db_file_ok(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 4)
        blocks_db.touch()  # 零字节新库（无表）≠ 损坏
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["ok"] is True
        assert result["issues"] == []
        assert result["repaired"] is False

    def test_consistent_blocks_pass(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 4)
        upsert_blocks([_block(1, 1, 2, 2), _block(2, 3, 4, 2)], blocks_db)
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result == {"ok": True, "issues": [], "repaired": False}
        assert len(load_all(blocks_db)) == 2  # 未触发重建，块原样保留

    def test_both_empty_ok(self, blocks_db, messages_db):
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result == {"ok": True, "issues": [], "repaired": False}


# ---------------------------------------------------------------------------
# 2. 漂移注入（DB 删行）→ 检测 + 整库重建
# ---------------------------------------------------------------------------

class TestDriftRebuild:
    def test_deleted_row_detected_and_rebuilt(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 8)  # 4 单元
        upsert_blocks([_block(1, 1, 2, 2), _block(2, 3, 4, 2)], blocks_db)
        assert integrity.check_blocks_integrity(blocks_db, messages_db)["issues"] == []

        _delete_row(messages_db, 2)  # 漂移：删掉块#1 的 end 端点行

        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["ok"] is True
        assert result["repaired"] is True
        assert any("不存在" in i for i in result["issues"])
        assert any("count" in i for i in result["issues"])

        # 重建结果（水位线语义）= 当前 DB 全量重切：7 行（删了 rowid2）→
        # r1 与 r3 连续 user 合并同单元 → 共 3 单元 ≤ 保留轮数 3 → 零块
        rebuilt = load_all(blocks_db)
        assert rebuilt == []

        # 复检一致
        recheck = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert recheck == {"ok": True, "issues": [], "repaired": False}

    def test_all_messages_deleted_rebuilds_to_empty(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 4)
        upsert_blocks([_block(1, 1, 2, 2)], blocks_db)
        for rid in (1, 2, 3, 4):
            _delete_row(messages_db, rid)
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["ok"] is True
        assert result["repaired"] is True
        assert load_all(blocks_db) == []


# ---------------------------------------------------------------------------
# 3. 损坏文件 → 删文件重建
# ---------------------------------------------------------------------------

class TestCorruptFile:
    def test_garbage_blocks_file_rebuilt(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 4)  # 2 单元，单元成本各 36
        blocks_db.write_bytes(b"\x00garbage-not-sqlite\xff" * 64)

        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["ok"] is True
        assert result["repaired"] is True
        assert any("blocks_db_corrupt" in i for i in result["issues"])

        # 水位线语义：2 单元 ≤ 保留轮数 3 → 全部保留、零块
        rebuilt = load_all(blocks_db)
        assert rebuilt == []

        recheck = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert recheck == {"ok": True, "issues": [], "repaired": False}


# ---------------------------------------------------------------------------
# 4. 检测失败 ≠ 损坏（防 launcher 闩锁误触）
# ---------------------------------------------------------------------------

class TestCheckFailed:
    def test_detection_exception_not_treated_as_corruption(
        self, blocks_db, messages_db, monkeypatch
    ):
        _create_messages_db(messages_db, 4)
        upsert_blocks([_block(1, 1, 2, 2)], blocks_db)

        def _boom(_path):
            raise RuntimeError("simulated read failure")

        monkeypatch.setattr(integrity, "_read_message_rows", _boom)
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["ok"] is True  # 检测失败不判损坏
        assert result["check_failed"] is True
        assert "simulated read failure" in result["error"]
        assert result["repaired"] is False
        assert len(load_all(blocks_db)) == 1  # 块表未被重建触碰


# ---------------------------------------------------------------------------
# 5. 区间重叠/倒置 → 检测 + 重建
# ---------------------------------------------------------------------------

class TestRangeViolations:
    def test_overlap_detected_and_rebuilt(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 6)
        upsert_blocks([_block(1, 1, 3, 3), _block(2, 3, 6, 4)], blocks_db)
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["repaired"] is True
        assert any("重叠" in i or "非单调" in i for i in result["issues"])
        recheck = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert recheck["issues"] == []

    def test_inverted_range_detected(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 4)
        upsert_blocks([_block(1, 3, 2, 2)], blocks_db)  # start > end
        result = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert result["repaired"] is True
        assert any("倒置" in i for i in result["issues"])


# ---------------------------------------------------------------------------
# 6. /new 清理面（spec §4）
# ---------------------------------------------------------------------------

class TestResetDerivedState:
    def test_new_cleanup_surface(self, blocks_db, tmp_path, monkeypatch):
        upsert_blocks([_block(1, 1, 2, 2)], blocks_db)
        cal_path = tmp_path / "token_calibration.json"
        calibration.update_ratio(200, 100, cal_path)  # ratio=2.0 落盘 + 缓存
        assert cal_path.exists()
        assert AUTO_GATE.try_acquire(0.9) is True  # 闩锁

        cm_calls = []

        class _FakeCM:
            def set_system_token_estimate(self, n):
                cm_calls.append(n)

        import agent.context_manager as cm_module

        monkeypatch.setattr(cm_module, "_context_manager", _FakeCM())

        try:
            reset_derived_state(blocks_db, cal_path)
            assert not blocks_db.exists()  # 块库文件删除
            assert not cal_path.exists()  # 倍率文件删除
            assert calibration.get_ratio(cal_path) == DEFAULT_RATIO  # 缓存复位
            assert AUTO_GATE.try_acquire(0.9) is True  # 闩锁已解除可再触发
            assert cm_calls == [0]  # 内存 system 估算作废
        finally:
            AUTO_GATE.release()  # 不污染同进程其他测试

    def test_reset_idempotent_when_nothing_exists(self, tmp_path):
        reset_derived_state(tmp_path / "nonexistent.db", tmp_path / "nonexistent.json")
        # 不抛异常即通过


# ---------------------------------------------------------------------------
# 7. 整库重建水位线语义：重建 = 全部单元按「留最近 keepRecentTurns 轮」归档
# ---------------------------------------------------------------------------

class TestRebuildWatermark:
    def test_rebuild_archives_all_but_keep_turns(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 8)  # 4 单元，留 3 → 首单元归档
        n = integrity._rebuild(blocks_db, messages_db)

        assert n == 1
        rebuilt = load_all(blocks_db)
        assert len(rebuilt) == 1
        assert (rebuilt[0].start_rowid, rebuilt[0].end_rowid, rebuilt[0].count) == (1, 2, 2)

        # 复检一致（重建产物满足全部校验项）
        recheck = integrity.check_blocks_integrity(blocks_db, messages_db)
        assert recheck == {"ok": True, "issues": [], "repaired": False}

    def test_rebuild_few_units_keeps_all_no_blocks(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 6)  # 3 单元 ≤ keep=3 → 零块
        assert integrity._rebuild(blocks_db, messages_db) == 0
        assert load_all(blocks_db) == []

    def test_rebuild_empty_messages_db(self, blocks_db, messages_db):
        _create_messages_db(messages_db, 0)
        assert integrity._rebuild(blocks_db, messages_db) == 0

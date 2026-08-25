"""块摘要增强测试（agent/context_assembler/summarizer.py）。

全部 mock LLM（禁真实调用）；块 DB / 消息 DB / 用户配置均用 tmp_path 隔离真实 ~/.niu。
覆盖：成功回写 done、失败保 pending 不抛出、开关默认关闭零调用、限流上限 N=5、
空闲判定跳过本轮、>20K 输入截断头尾保留、索引行摘要渲染（尺寸不变式）。
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent.context_assembler import blocks as blocks_module
from agent.context_assembler import summarizer as sm
from agent.context_assembler.blocks import PointerBlock, upsert_blocks

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "context_blocks.db"


@pytest.fixture
def messages_db(tmp_path):
    """临时消息库：3 条消息（rowid 1-3）。"""
    p = tmp_path / "messages.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE messages (role TEXT, content TEXT, tool_call_id TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO messages (role, content, tool_call_id, created_at) VALUES (?, ?, ?, ?)",
        [
            ("user", "帮我把HN抓取改成中文摘要", None, "2026-08-20T10:00:00"),
            ("assistant", "已修改抓取脚本并加入翻译步骤", None, "2026-08-20T10:01:00"),
            ("tool", "scrape result...", "call_1", "2026-08-20T10:01:30"),
        ],
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """用户配置注入点：返回写入函数与路径。"""
    p = tmp_path / "user-config.json"

    def _write(context: dict[str, Any]) -> Path:
        p.write_text(json.dumps({"context": context}), encoding="utf-8")
        return p

    monkeypatch.setattr(sm, "_config_path", lambda: p)
    # 预写一份空 context（默认禁用）
    _write({})
    return _write


def make_block(bid: int, start_rowid: int = 1, end_rowid: int = 3, **over: Any) -> PointerBlock:
    kwargs: dict[str, Any] = {
        "id": bid,
        "start_msg_id": f"msg-{bid}-start",
        "end_msg_id": f"msg-{bid}-end",
        "start_rowid": start_rowid,
        "end_rowid": end_rowid,
        "count": end_rowid - start_rowid + 1,
        "time_start": f"2026-08-{10 + bid}T10:00:00",
        "time_end": f"2026-08-{10 + bid}T11:00:00",
        "first_user": f"第{bid}块的首问内容",
    }
    kwargs.update(over)
    return PointerBlock(**kwargs)


# ---------------------------------------------------------------------------
# 开关与空闲门控
# ---------------------------------------------------------------------------

class TestGates:
    def test_disabled_by_default_zero_calls(self, db_path, messages_db, config_file, monkeypatch):
        """context.blockSummaryEnabled 缺省 false：零 LLM 调用、零状态变更。"""
        upsert_blocks([make_block(1)], db_path)

        def _boom(_prompt):
            raise AssertionError("禁用时不得发起 LLM 调用")

        monkeypatch.setattr(sm, "_bare_llm_call", _boom)
        assert sm.process_pending_blocks(db_path, messages_db) == 0
        assert blocks_module.load_all(db_path)[0].summary_state == "pending"

    def test_enabled_via_config(self, db_path, messages_db, config_file, monkeypatch):
        config_file({"blockSummaryEnabled": True})
        assert sm.read_summary_enabled() is True

    def test_corrupt_config_disables(self, tmp_path, monkeypatch):
        p = tmp_path / "bad.json"
        p.write_text("not-json", encoding="utf-8")
        monkeypatch.setattr(sm, "_config_path", lambda: p)
        assert sm.read_summary_enabled() is False

    def test_active_conversation_skips_round(self, db_path, messages_db, config_file, monkeypatch):
        """活跃对话期（非空闲）：跳过本轮，零调用。"""
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(1)], db_path)

        def _boom(_prompt):
            raise AssertionError("非空闲时不得发起 LLM 调用")

        monkeypatch.setattr(sm, "_bare_llm_call", _boom)
        assert sm.process_pending_blocks(db_path, messages_db, idle_check=lambda: False) == 0
        assert blocks_module.load_all(db_path)[0].summary_state == "pending"


# ---------------------------------------------------------------------------
# 摘要回写语义
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_success_writes_done(self, db_path, messages_db, config_file, monkeypatch):
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(1)], db_path)
        monkeypatch.setattr(
            sm, "_bare_llm_call",
            lambda _prompt: " 用户请求把HN抓取改为中文摘要，已完成并验证。\n第二行 ",
        )
        assert sm.process_pending_blocks(db_path, messages_db, idle_check=lambda: True) == 1
        b = blocks_module.load_all(db_path)[0]
        assert b.summary_state == "done"
        assert b.summary == "用户请求把HN抓取改为中文摘要，已完成并验证。 第二行"  # 空白归一

    def test_llm_none_keeps_pending(self, db_path, messages_db, config_file, monkeypatch):
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(1)], db_path)
        monkeypatch.setattr(sm, "_bare_llm_call", lambda _prompt: None)
        assert sm.process_pending_blocks(db_path, messages_db, idle_check=lambda: True) == 0
        assert blocks_module.load_all(db_path)[0].summary_state == "pending"

    def test_exception_keeps_pending_no_raise(self, db_path, messages_db, config_file, monkeypatch):
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(1)], db_path)

        def _raise(_prompt):
            raise RuntimeError("network down")

        monkeypatch.setattr(sm, "_bare_llm_call", _raise)
        assert sm.process_pending_blocks(db_path, messages_db, idle_check=lambda: True) == 0
        assert blocks_module.load_all(db_path)[0].summary_state == "pending"

    def test_failed_block_does_not_block_batch(self, db_path, messages_db, config_file, monkeypatch):
        """批量内单块失败不影响其余块。"""
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(1), make_block(2)], db_path)
        calls = []

        def _flaky(_prompt):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return "正常摘要"

        monkeypatch.setattr(sm, "_bare_llm_call", _flaky)
        assert sm.process_pending_blocks(db_path, messages_db, idle_check=lambda: True) == 1
        states = {b.id: b.summary_state for b in blocks_module.load_all(db_path)}
        assert states == {1: "pending", 2: "done"}

    def test_batch_limit_n5(self, db_path, messages_db, config_file, monkeypatch):
        """每轮上限 N=5：7 个 pending 只处理 5 个。"""
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(i) for i in range(1, 8)], db_path)
        calls = []

        def _count(_prompt):
            calls.append(1)
            return "摘要"

        monkeypatch.setattr(sm, "_bare_llm_call", _count)
        assert sm.process_pending_blocks(db_path, messages_db, idle_check=lambda: True) == 5
        assert len(calls) == 5
        states = {b.id: b.summary_state for b in blocks_module.load_all(db_path)}
        assert [i for i, s in states.items() if s == "done"] == [1, 2, 3, 4, 5]
        assert [i for i, s in states.items() if s == "pending"] == [6, 7]

    def test_writeback_preserves_other_fields(self, db_path, messages_db, config_file, monkeypatch):
        """回写只改摘要两字段，其余字段（实体标签等）保持不变。"""
        config_file({"blockSummaryEnabled": True})
        upsert_blocks([make_block(1, entities=["咖啡机定时任务"])], db_path)
        monkeypatch.setattr(sm, "_bare_llm_call", lambda _prompt: "摘要内容")
        sm.summarize_block(blocks_module.load_all(db_path)[0], db_path, messages_db)
        b = blocks_module.load_all(db_path)[0]
        assert b.entities == ["咖啡机定时任务"]
        assert b.first_user == "第1块的首问内容"
        assert b.summary == "摘要内容"


# ---------------------------------------------------------------------------
# 有界输入与索引行渲染
# ---------------------------------------------------------------------------

class TestBoundedInputAndRender:
    def test_load_block_text_over_20k_truncated_head_tail(self, tmp_path):
        """>20K 字符输入截断头尾各半保留，含中间省略标记。"""
        p = tmp_path / "big.db"
        conn = sqlite3.connect(p)
        conn.execute(
            "CREATE TABLE messages (role TEXT, content TEXT, tool_call_id TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO messages VALUES ('user', ?, NULL, '2026-08-20T10:00:00')",
            ("HEAD" + "甲" * 12000,),
        )
        conn.execute(
            "INSERT INTO messages VALUES ('assistant', ?, NULL, '2026-08-20T10:01:00')",
            ("TAIL" + "乙" * 9900,),
        )
        conn.commit()
        conn.close()
        text = sm._load_block_text(make_block(1, 1, 2), p)
        assert len(text) <= sm.MAX_INPUT_CHARS + 100  # 上限 + 标记余量
        assert "HEAD" in text and "TAIL" in text
        assert "中间内容已省略" in text

    def test_normalize_summary_clamps_to_100(self):
        long_text = "词" * 300
        assert len(sm._normalize_summary(long_text)) == sm.SUMMARY_MAX_CHARS

    def test_render_index_done_uses_summary_line(self):
        from agent.context_manager import ContextManager

        blocks = [
            make_block(1, summary="用户配置了咖啡机定时任务并调试通过", summary_state="done"),
            make_block(2),
        ]
        text = ContextManager._render_index(blocks)
        lines = text.split("\n")
        assert "[块#1] 用户配置了咖啡机定时任务并调试通过" in lines
        # pending 保持机械行
        assert any("[块#2]" in ln and "首问:" in ln for ln in lines)

    def test_render_index_summary_line_size_invariant(self):
        """尺寸不变式：done 块的索引行摘要部分 ≤100 字。"""
        from agent.context_manager import ContextManager

        blocks = [make_block(1, summary="超" * 500, summary_state="done")]
        line = next(
            ln for ln in ContextManager._render_index(blocks).split("\n") if ln.startswith("[块#1]")
        )
        summary_part = line.removeprefix("[块#1] ")
        assert len(summary_part) <= 100

    def test_render_index_done_with_empty_summary_falls_back(self):
        """done 但摘要为空 → 回退机械行。"""
        from agent.context_manager import ContextManager

        blocks = [make_block(1, summary="", summary_state="done")]
        text = ContextManager._render_index(blocks)
        assert "首问:" in text

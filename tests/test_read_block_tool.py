"""read_history_block 工具 + 实体标签接通测试（Task 4 清单）。

覆盖：schema 断言 / 正常读取（含 tool_call_id 归属）/ 超大块头尾精简与
单条动态截断 / 坏块号错误信息（含有效范围提示）/ 空库「暂无归档历史」/
实体标签反查降级（图不可用→[]，归档失败不阻塞）。全部 mock/临时 DB，
无 LLM 调用、不碰真实 ~/.niu 数据。
"""

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(
    0, os.path.join(_PROJECT_ROOT, "mcp-servers", "session-manager", "src")
)

from niu_session_manager import TOOL_SCHEMAS, read_history_block  # noqa: E402

from agent.context_assembler.blocks import PointerBlock, upsert_blocks  # noqa: E402
from agent.context_assembler import compaction, entity_tags  # noqa: E402


# ============== 测试 DB 构造 ==============

_MESSAGES_DDL = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_results TEXT,
    tool_call_id TEXT,
    degraded_reason TEXT,
    created_at TEXT
)
"""


def _make_messages_db(path: Path, rows: list[dict]) -> None:
    """按给定 rowid 顺序写入消息行。rows 每项含 rowid/role/content/tool_call_id/created_at。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_MESSAGES_DDL.replace(
            "CREATE TABLE messages", "CREATE TABLE IF NOT EXISTS messages"))
        for r in rows:
            conn.execute(
                "INSERT INTO messages (rowid, id, role, content, tool_call_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    r["rowid"],
                    r.get("id", f"m{r['rowid']}"),
                    r["role"],
                    r.get("content", ""),
                    r.get("tool_call_id"),
                    r.get("created_at", "2026-08-20T10:00:00"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _block(bid, start_rowid, end_rowid, count=None, **kw):
    return PointerBlock(
        id=bid,
        start_msg_id=kw.get("start_msg_id", f"s{bid}"),
        end_msg_id=kw.get("end_msg_id", f"e{bid}"),
        start_rowid=start_rowid,
        end_rowid=end_rowid,
        count=count if count is not None else end_rowid - start_rowid + 1,
        time_start=kw.get("time_start", "2026-08-12T09:00:00"),
        time_end=kw.get("time_end", "2026-08-15T18:00:00"),
        entities=kw.get("entities", []),
        first_user=kw.get("first_user", "帮我把HN抓取改成中文摘要"),
    )


@pytest.fixture
def env(tmp_path):
    """一对临时 DB 路径（blocks 未写、messages 空）。"""
    blocks_db = tmp_path / "context_blocks.db"
    messages_db = tmp_path / "messages.db"
    conn = sqlite3.connect(str(messages_db))
    conn.execute(_MESSAGES_DDL)
    conn.commit()
    conn.close()
    return {"blocks_db_path": str(blocks_db), "messages_db_path": str(messages_db)}


def _call(env_kwargs, block_id):
    return read_history_block(block_id, **env_kwargs)


# ============== Schema 断言 ==============


class TestSchema:
    def test_read_history_block_in_tool_schemas(self):
        assert "read_history_block" in TOOL_SCHEMAS

    def test_schema_requires_integer_block_id(self):
        schema = TOOL_SCHEMAS["read_history_block"]
        props = schema["input_schema"]["properties"]
        assert props["block_id"]["type"] == "integer"
        assert schema["input_schema"]["required"] == ["block_id"]
        assert schema["name"] == "read_history_block"

    def test_schema_count_grew_to_five(self):
        assert len(TOOL_SCHEMAS) == 5

    def test_schema_description_carries_index_semantics(self):
        """通道改回 MCP static 后描述须自承载解码语义（niu.md 说明书已删）"""
        desc = TOOL_SCHEMAS["read_history_block"]["description"]
        assert "[历史索引]" in desc
        assert "行首方括号内的数字（如 [3]）即 block_id" in desc
        assert "精简" in desc

# ============== 正常读取 ==============


class TestNormalRead:
    def test_reads_rows_within_block_range(self, env):
        rows = [
            {"rowid": 10, "role": "user", "content": "帮我查一下天气"},
            {"rowid": 11, "role": "assistant",
             "content": "", "tool_calls": "[{\"id\": \"tc1\"}]"},
            {"rowid": 12, "role": "tool", "content": "晴，26度",
             "tool_call_id": "tc1"},
            {"rowid": 13, "role": "assistant", "content": "今天晴，26度。"},
        ]
        _make_messages_db(Path(env["messages_db_path"]), rows)
        upsert_blocks([_block(1, 10, 13)], Path(env["blocks_db_path"]))

        result = _call(env, 1)

        assert result["status"] == "ok"
        assert result["total_messages"] == 4
        text = result["text"]
        assert text.startswith("[1] ") and "2026-08-12T09:00:00" in text
        assert "帮我查一下天气" in text
        # tool 输出必须带 tool_call_id 归属
        assert "[tool·tc1]" in text and "晴，26度" in text

    def test_block_metadata_and_entities_in_output(self, env):
        _make_messages_db(Path(env["messages_db_path"]), [
            {"rowid": 1, "role": "user", "content": "hi"},
        ])
        upsert_blocks(
            [_block(3, 1, 1, entities=["咖啡机", "HomeAssistant"])],
            Path(env["blocks_db_path"]),
        )
        result = _call(env, 3)
        assert result["status"] == "ok"
        assert result["block"]["entities"] == ["咖啡机", "HomeAssistant"]
        assert "咖啡机/HomeAssistant" in result["text"]


# ============== 截断策略 ==============


class TestTruncation:
    def test_single_long_message_dynamically_truncated(self, env):
        n = 4
        long_content = "x" * 200000
        rows = [
            {"rowid": i + 1, "role": "user", "content": long_content}
            for i in range(n)
        ]
        _make_messages_db(Path(env["messages_db_path"]), rows)
        upsert_blocks([_block(1, 1, n)], Path(env["blocks_db_path"]))

        result = _call(env, 1)
        assert result["status"] == "ok"
        cap = result["per_message_char_limit"]
        # 对齐 read_file 规格：单条上限落在 [100, 10000] 区间
        assert 100 <= cap <= 10000
        assert "... [TRUNCATED]" in result["text"]

    def test_huge_block_head_tail_kept_with_marker(self, env):
        n = 1200
        rows = [
            {"rowid": i + 1, "role": "user", "content": f"msg {i + 1}"}
            for i in range(n)
        ]
        _make_messages_db(Path(env["messages_db_path"]), rows)
        upsert_blocks([_block(1, 1, n)], Path(env["blocks_db_path"]))

        result = _call(env, 1)
        assert result["status"] == "ok"
        assert result["head_tail_truncated"] is True
        assert result["omitted_messages"] == n - 500
        assert result["rendered_messages"] == 500
        text = result["text"]
        assert "[已精简：中间省略 700 条消息]" in text
        assert "msg 1\n" in text   # 头部保留的第一条
        assert "msg 250" in text   # 头部保留的最后一条
        assert "msg 251" not in text  # 中间被省略
        assert "msg 1200" in text  # 尾部保留


# ============== 错误路径 ==============


class TestErrors:
    def test_empty_block_store_reports_no_archive(self, env):
        result = _call(env, 1)
        assert result["status"] == "error"
        assert "暂无归档历史" in result["error"]

    def test_bad_block_id_lists_valid_range(self, env):
        upsert_blocks([_block(5, 1, 2), _block(9, 3, 4)], Path(env["blocks_db_path"]))
        result = _call(env, 7)
        assert result["status"] == "error"
        assert "归档块 7 不存在" in result["error"]
        assert "5~9" in result["error"] and "共 2 块" in result["error"]

    def test_non_integer_block_id_rejected(self, env):
        upsert_blocks([_block(1, 1, 2)], Path(env["blocks_db_path"]))
        result = _call(env, "abc")
        assert result["status"] == "error"
        assert "整数" in result["error"]


# ============== 实体标签接通与降级 ==============


class TestEntityTags:
    def test_dates_covered_parses_iso_range(self):
        days = entity_tags._dates_covered("2026-08-12T09:00:00", "2026-08-14T18:00:00")
        assert days == ["2026-08-12", "2026-08-13", "2026-08-14"]

    def test_dates_covered_invalid_input_returns_empty(self):
        assert entity_tags._dates_covered("garbage", "") == []

    def test_tags_for_range_expands_session_entities(self):
        class FakeGraph:
            def __init__(self, nodes, edges):
                self._nodes, self._edges = nodes, edges

            def has_node(self, n):
                return n in self._nodes

            def __getitem__(self, n):  # 邻接视图（与 networkx 一致）
                return self._edges.get(n)

        graph = FakeGraph(
            nodes={"2026-08-12会话"},
            edges={
                "2026-08-12会话": {
                    "咖啡机定时任务": {"weight": 3.0},
                    "HomeAssistant": {"weight": 5.0},
                    "2026-08-11会话": {"weight": 9.0},   # 会话实体应被排除
                    "niu": {"weight": 9.0},               # 根节点应被排除
                },
            },
        )
        tags = entity_tags.tags_for_range(
            graph, "2026-08-12T00:00:00", "2026-08-12T23:59:59"
        )
        # 权重降序、排除会话实体与根节点
        assert tags == ["HomeAssistant", "咖啡机定时任务"]

    def test_tags_for_range_caps_at_three(self):
        class FakeGraph:
            def has_node(self, n):
                return True

            def __getitem__(self, n):  # 邻接视图（与 networkx 一致）
                return {f"实体{i}": {"weight": 1.0} for i in range(10)}

        tags = entity_tags.tags_for_range(
            FakeGraph(), "2026-08-12T00:00:00", "2026-08-12T23:59:59"
        )
        assert len(tags) == entity_tags.MAX_TAGS_PER_BLOCK == 3

    def test_collect_tags_degrades_to_empty_without_graph(self):
        with patch.object(entity_tags, "_graph_snapshot", return_value=None):
            assert entity_tags.collect_tags([("2026-08-12", "2026-08-12")]) == [[]]

    def test_collect_tags_never_raises(self):
        with patch.object(
            entity_tags, "_graph_snapshot", side_effect=RuntimeError("boom")
        ):
            result = entity_tags.collect_tags([("a", "b"), ("c", "d")])
            assert result == [[], []]

    def test_archive_excluded_units_fills_tags_but_survives_failure(
        self, tmp_path, monkeypatch
    ):
        """归档时回填标签；collect_tags 抛异常也不阻塞归档。"""
        from types import SimpleNamespace

        def msg(role, content, mid, rowid, created_at="2026-08-12T10:00:00"):
            return SimpleNamespace(
                id=mid, rowid=rowid, role=role, content=content,
                tool_calls=None, tool_call_id=None, created_at=created_at,
            )

        messages = [msg("user", "q1", "u1", 1), msg("assistant", "a1", "a1", 2)]
        units = [(0, 1)]

        monkeypatch.setattr(
            entity_tags, "collect_tags", lambda ranges: [["实体A"]] * len(ranges)
        )
        db = tmp_path / "b.db"
        added = compaction.archive_excluded_units(messages, units, 99, db)
        assert added == 1
        from agent.context_assembler.blocks import load_all
        assert load_all(db)[0].entities == ["实体A"]

        monkeypatch.setattr(
            entity_tags, "collect_tags", lambda ranges: (_ for _ in ()).throw(
                RuntimeError("boom"))
        )
        db2 = tmp_path / "b2.db"
        assert compaction.archive_excluded_units(messages, units, 99, db2) == 1
        assert load_all(db2)[0].entities == []

"""fold_tool_output 工具测试（Task 3：7 处触点）。

覆盖：schema 断言 / 单条与多条折叠 / 不存在 rowid / role≠tool / 已折叠幂等
（全幂等返回 status:ok 不报错，spec §6）/ 部分成功附 errors / 空列表 /
释放合计文案 / 旧行无占比快照说明子串（新折叠行含 output_pct NULL 时追加，部分成功也追加）/ 迁移失败降级（fold_columns_available False → 明确错误文案）。
全部 tmp DB（经 kwargs 注入），无 LLM 调用、不碰真实 ~/.niu 数据。
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

from niu_session_manager import TOOL_SCHEMAS, fold_tool_output  # noqa: E402


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
    created_at TEXT,
    folded INTEGER DEFAULT 0,
    output_pct REAL DEFAULT NULL
)
"""


def _make_messages_db(path: Path, rows: list[dict]) -> None:
    """rows 每项含 rowid/role/content/output_pct；按 rowid 顺序写入。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_MESSAGES_DDL.replace(
            "CREATE TABLE messages", "CREATE TABLE IF NOT EXISTS messages"))
        for r in rows:
            conn.execute(
                "INSERT INTO messages (rowid, id, role, content, output_pct, folded)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    r["rowid"],
                    r.get("id", f"m{r['rowid']}"),
                    r["role"],
                    r.get("content", ""),
                    r.get("output_pct"),
                    r.get("folded", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _get_folded(path: Path) -> dict[int, int]:
    conn = sqlite3.connect(str(path))
    try:
        return dict(conn.execute("SELECT rowid, folded FROM messages").fetchall())
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path):
    """tmp messages.db，含 4 条消息：tool×3（一条已折叠、一条无 pct）+ user×1。"""
    db = tmp_path / "messages.db"
    _make_messages_db(db, [
        {"rowid": 10, "role": "tool", "content": "文件内容A", "output_pct": 4.2},
        {"rowid": 11, "role": "user", "content": "帮我看下这个文件"},
        {"rowid": 12, "role": "tool", "content": "检索结果B", "output_pct": None},
        {"rowid": 13, "role": "tool", "content": "已有折叠C", "output_pct": 7.5,
         "folded": 1},
    ])
    return {"messages_db_path": str(db), "db": db}


def _call(env_kwargs, output_ids):
    return fold_tool_output(output_ids, **env_kwargs)


# ============== Schema 断言 ==============


class TestSchema:
    def test_fold_tool_in_tool_schemas(self):
        assert "fold_tool_output" in TOOL_SCHEMAS

    def test_schema_shape(self):
        schema = TOOL_SCHEMAS["fold_tool_output"]
        props = schema["input_schema"]["properties"]
        assert props["output_ids"]["type"] == "array"
        assert props["output_ids"]["items"]["type"] == "integer"
        assert schema["input_schema"]["required"] == ["output_ids"]
        assert schema["name"] == "fold_tool_output"

    def test_schema_count_grew_to_six(self):
        assert len(TOOL_SCHEMAS) == 6

    def test_description_carries_piggyback_teaching(self):
        """description 含搭车调用教学（计划 Task 3 Step 3 原文）"""
        desc = TOOL_SCHEMAS["fold_tool_output"]["description"]
        assert "编号=窗口内 tool 输出头行" in desc
        assert "搭车调用" in desc
        assert "绝不要只为折叠单开一轮" in desc
        assert "重新调用原工具" in desc


# ============== 正常折叠 ==============


class TestFold:
    def test_single_fold(self, env):
        result = _call(env, [10])
        assert result["status"] == "ok"
        assert result["folded"] == [10]
        assert result["freed_pct"] == 4.2
        assert "已折叠 1 条输出（#10）" in result["message"]
        assert "释放约 4.2%" in result["message"]
        # 10 有占比快照 → 不追加旧行说明
        assert "未含占比快照" not in result["message"]
        assert _get_folded(env["db"])[10] == 1

    def test_multiple_folds(self, env):
        result = _call(env, [10, 12])
        assert result["status"] == "ok"
        assert result["folded"] == [10, 12]
        # 12 无 pct → freed 只算 4.2
        assert result["freed_pct"] == 4.2
        # 新折叠行含 1 条 output_pct NULL（#12）→ 追加旧行说明
        assert "含 1 条升级前旧输出，未含占比快照，不计入释放估算" in result["message"]
        folded = _get_folded(env["db"])
        assert folded[10] == 1 and folded[12] == 1

    def test_nonexistent_rowid(self, env):
        result = _call(env, [999])
        assert result["status"] == "error"
        assert "输出#999 不存在" in result["error"]
        assert "read_history_block" in result["error"]

    def test_non_tool_role(self, env):
        result = _call(env, [11])
        assert result["status"] == "error"
        assert "输出#11 不是工具输出" in result["error"]
        assert _get_folded(env["db"])[11] == 0

    def test_partial_success_reports_errors(self, env):
        result = _call(env, [10, 999])
        assert result["status"] == "ok"
        assert result["folded"] == [10]
        assert len(result["errors"]) == 1
        assert "1 条未成功" in result["message"]
        # 新折叠行 #10 有占比快照（k=0）→ 即使部分成功也不追加旧行说明
        assert "未含占比快照" not in result["message"]
        assert _get_folded(env["db"])[10] == 1

    def test_partial_success_with_null_pct_appends_note(self, env):
        """部分成功且新折叠行含 output_pct NULL（k>0）→ errors 与旧行说明都追加（R3-B P3）"""
        result = _call(env, [12, 999])
        assert result["status"] == "ok"
        assert result["folded"] == [12]
        assert len(result["errors"]) == 1
        assert "1 条未成功" in result["message"]
        assert "含 1 条升级前旧输出，未含占比快照，不计入释放估算" in result["message"]
        assert _get_folded(env["db"])[12] == 1


# ============== 幂等（spec §6） ==============


class TestIdempotent:
    def test_already_folded_is_note_not_error(self, env):
        result = _call(env, [13])
        assert result["status"] == "ok"
        assert result["folded"] == []
        assert any("输出#13 已是折叠状态" in n for n in result.get("notes", []))
        assert not result.get("errors")

    def test_all_idempotent_returns_ok(self, env):
        """全幂等 → status:ok + folded:[]，不报错（spec §6）"""
        result = _call(env, [13, 13])
        assert result["status"] == "ok"
        assert result["folded"] == []
        assert len(result.get("notes", [])) == 2


# ============== 边界与降级 ==============


class TestEdge:
    def test_empty_list(self, env):
        result = _call(env, [])
        assert result["status"] == "error"
        assert "不能为空列表" in result["error"]

    def test_migration_failure_degradation(self, env):
        """fold_columns_available False → 明确错误文案（R2 双审偏离①）"""
        with patch("agent.session.fold_columns_available", return_value=False):
            result = _call(env, [10])
        assert result["status"] == "error"
        assert "折叠功能不可用" in result["error"]
        assert "迁移失败" in result["error"]
        # 未写入任何标志（13 是预置折叠行，不在检查范围）
        folded = _get_folded(env["db"])
        assert folded[10] == 0 and folded[12] == 0

    def test_db_write_failure_returns_error(self, env):
        """sqlite3 写块异常 → status:error（不抛给调用方）"""
        with patch("agent.session.fold_columns_available", return_value=True):
            result = fold_tool_output([10], messages_db_path=str(env["db"] / "no_such_dir" / "x.db"))
        assert result["status"] == "error"
        assert "DB 写入失败" in result["error"]

    def test_non_test_env_clears_kwargs_injection(self, env, monkeypatch):
        """T3 质量审 P2：非测试环境（无 pytest）清空注入通道——LLM 幻觉不可重定向 DB 写路径。"""
        import sys as _sys
        import niu_session_manager as nsm
        monkeypatch.delitem(_sys.modules, "pytest")  # 模拟生产环境
        captured = {}

        def _rec(kw):
            captured["kw"] = kw
            return ("", str(env["db"]))  # 回落到 tmp 路径：仅 SELECT，无写风险

        monkeypatch.setattr(nsm, "_resolve_db_paths", _rec)
        result = fold_tool_output([999], messages_db_path=env["messages_db_path"])
        assert captured["kw"] == {}  # 注入在路径解析前被清空
        assert result["status"] == "error" and "不存在" in result["error"]

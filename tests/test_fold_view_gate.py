"""T2b-2：fold_tool_output 视野门（接受集 ⊆ 当前上下文视野）。

用户定案：**判定序视野优先于幂等**——编号不在当前上下文视野内（模型看不见的
rowid 猜测值）一律拒绝，不查 DB、不看 folded、不累加 freed；报错只回一行规则，
不回显坏编号、不发编号清单（"可用 read_history_block 查历史块"整句已删）。

覆盖：
1. 视野未建立（None）→ 直接拒绝，DB 零改动
2. 编号不在视野 → 拒绝，freed 不累加（视野外行的 output_pct 绝不进释放合计）
3. 编号在视野但非 tool 行 / DB 无此行 → 拒绝
4. 视野内已折叠 → notes + status:ok（幂等语义未被视野门吃掉）
5. 混合（成功 + 视野外）→ ok + 成功列表 + errors 为**一条聚合文案**，
   且全文不含坏编号（含位数边界：1/11、7/70、10/100 等互为子串的参数化）
6. 模块源码内已无 read_history_block 指引句

基建同 tests/test_fold_tool.py 同款 **kwargs tmp DB 注入通道（禁真实
~/.niu/messages.db）+ 单点 patch 视野函数；无 LLM 调用。
"""

import contextlib
import json
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

import niu_session_manager as nsm  # noqa: E402
from niu_session_manager import fold_tool_output  # noqa: E402

# ============== 规格文案（与实现逐字对齐） ==============

NO_FOLDABLE_ERROR = (
    "没有可折叠的输出：指定编号不在当前上下文视野内或不是工具输出"
    "（只可折叠编号出现在当前上下文中的工具输出）"
)


def aggregate_error(n: int) -> str:
    return f"{n} 条编号不在当前上下文视野内或不是工具输出（未折叠）"


# ============== 视野注入 helper（与 test_fold_tool.py 同签名） ==============


@contextlib.contextmanager
def visible(rowids):
    """注入视野：rowids=None=视野未建立（尚未组装过）；set=视野内工具 rowid。

    单点 patch 入口——真实实现只在一个函数里查 ContextManager，测试只改这一个点。
    """
    with patch.object(nsm, "_current_visible_tool_rowids", return_value=rowids):
        yield


# ============== 测试 DB 构造（同 test_fold_tool.py 制式） ==============

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


def _snapshot(path: Path) -> list[tuple]:
    """整表快照（rowid, role, content, folded, output_pct）——证明「零改动」。

    只看 folded 列不够：视野门必须连 SELECT/UPDATE 都不落到视野外行上，
    整表逐行比对才能证明没有被写、没有释放量被偷偷累加。
    """
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT rowid, role, content, folded, output_pct FROM messages ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path):
    """tmp messages.db：tool×3（12 无 pct 快照、13 已折叠）+ user×1（rowid 11）。"""
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


# ============== ① 视野未建立 ==============


class TestViewNotEstablished:
    def test_refuses_without_touching_db(self, env):
        before = _snapshot(env["db"])
        with visible(None):
            result = _call(env, [10, 12, 13])
        assert result == {"status": "error", "error": NO_FOLDABLE_ERROR}
        assert _snapshot(env["db"]) == before, "视野未建立时不得写任何行"

    def test_no_freed_reported(self, env):
        with visible(None):
            result = _call(env, [10])
        assert "freed_pct" not in result and "folded" not in result

    def test_columns_unavailable_checked_before_view(self, env):
        """判定序①：折叠列迁移失败优先于视野门——两者都坏时报「不可用」而非视野文案。"""
        before = _snapshot(env["db"])
        with patch("agent.session.fold_columns_available", return_value=False):
            with visible(None):
                result = _call(env, [10])
        assert result["status"] == "error"
        assert "折叠功能不可用" in result["error"]
        assert _snapshot(env["db"]) == before


# ============== ② 编号不在视野 ==============


class TestOutOfView:
    def test_out_of_view_refused_and_db_untouched(self, env):
        before = _snapshot(env["db"])
        with visible({10, 12}):
            result = _call(env, [13])
        assert result == {"status": "error", "error": NO_FOLDABLE_ERROR}
        assert _snapshot(env["db"]) == before

    def test_out_of_view_pct_never_enters_freed(self, env):
        """视野外行（13, pct=7.5）的占比绝不进释放合计——只累加成功行。"""
        with visible({10}):
            result = _call(env, [10, 13])
        assert result["status"] == "ok"
        assert result["folded"] == [10]
        assert result["freed_pct"] == 4.2, "视野外行的 7.5% 被偷算了"
        assert _snapshot(env["db"])[3][3] == 1  # rowid 13 折叠态未变

    def test_out_of_view_unknown_rowid_refused(self, env):
        """模型猜测的 DB 里根本不存在的编号 → 同样按视野外拒绝，不回显编号。"""
        before = _snapshot(env["db"])
        with visible({10}):
            result = _call(env, [999])
        assert result == {"status": "error", "error": NO_FOLDABLE_ERROR}
        assert "999" not in json.dumps(result, ensure_ascii=False)
        assert _snapshot(env["db"]) == before


# ============== ③ 视野内但不是可折叠的工具输出 ==============


class TestInViewButNotFoldable:
    def test_non_tool_row_refused(self, env):
        """人工注入不变量破坏：user 行 rowid 落在视野集内也必须拒（非 tool）。"""
        before = _snapshot(env["db"])
        with visible({11}):
            result = _call(env, [11])
        assert result == {"status": "error", "error": NO_FOLDABLE_ERROR}
        assert _snapshot(env["db"]) == before

    def test_missing_row_in_view_refused(self, env):
        """视野集里有、DB 里没有（并发归档竞态）→ 拒绝，不写库。"""
        before = _snapshot(env["db"])
        with visible({10, 999}):
            result = _call(env, [10, 999])
        assert result["status"] == "ok" and result["folded"] == [10]
        assert result["errors"] == [aggregate_error(1)]
        assert "999" not in json.dumps(result, ensure_ascii=False)
        # 只有成功行被写；DB 里不存在的 999 谈不上改动，快照仅在 10 上变化
        after = _snapshot(env["db"])
        assert after[:1] != before[:1] and after[1:] == before[1:]


# ============== ④ 视野内已折叠 ==============


class TestInViewAlreadyFolded:
    def test_note_not_error(self, env):
        with visible({13}):
            result = _call(env, [13])
        assert result["status"] == "ok"
        assert result["folded"] == []
        assert any("已是折叠状态" in n for n in result["notes"])
        assert result["errors"] == []

    def test_view_gate_does_not_eat_idempotency(self, env):
        """视野内重复编号（[13,13]）→ 全 notes，status:ok（与旧幂等语义同制式）。"""
        with visible({13}):
            result = _call(env, [13, 13])
        assert result["status"] == "ok"
        assert result["folded"] == []
        assert len(result["notes"]) == 2
        assert result["errors"] == []


# ============== ⑤ 混合：成功 + 视野外 ==============


class TestMixed:
    @pytest.mark.parametrize("good,bad", [(1, 11), (7, 70), (10, 100), (3, 30)])
    def test_aggregate_error_never_echoes_bad_id(self, tmp_path, good, bad):
        """位数边界：好编号是坏编号的子串时（1⊂11、7⊂70、10⊂100），坏编号仍不得出现。"""
        db = tmp_path / f"m{good}.db"
        _make_messages_db(db, [
            {"rowid": good, "role": "tool", "content": "好输出", "output_pct": 1.5},
            {"rowid": bad, "role": "tool", "content": "坏输出", "output_pct": 99.9},
        ])
        env = {"messages_db_path": str(db), "db": db}
        with visible({good}):
            result = _call(env, [good, bad])
        assert result["status"] == "ok"
        assert result["folded"] == [good]
        assert result["errors"] == [aggregate_error(1)]
        assert result["freed_pct"] == 1.5
        text = json.dumps(result, ensure_ascii=False)
        assert f"#{good}" in text, "成功编号应保留在 message 里"
        assert str(bad) not in text, f"错误文案泄露了坏编号 {bad}"
        assert _snapshot(db)[1][3] == 0, "视野外行不得被写"

    def test_two_refusals_aggregate_into_one_entry(self, env):
        with visible({10}):
            result = _call(env, [10, 12, 13])
        assert result["status"] == "ok"
        assert result["folded"] == [10]
        assert result["errors"] == [aggregate_error(2)], "拒项须聚合成一条，不发清单"
        text = json.dumps(result, ensure_ascii=False)
        assert "12" not in text and "13" not in text
        assert "1 条未成功" in result["message"]

    def test_notes_and_errors_coexist(self, env):
        with visible({10, 13}):
            result = _call(env, [10, 13, 12])
        assert result["status"] == "ok"
        assert result["folded"] == [10]
        assert len(result["notes"]) == 1 and "13" in result["notes"][0]
        assert result["errors"] == [aggregate_error(1)]
        assert "12" not in json.dumps(result["errors"], ensure_ascii=False)


# ============== ⑥ 文案纪律 ==============


class TestTextDiscipline:
    def test_module_source_has_no_read_history_block_hint(self):
        """fold 报错里的「可用 read_history_block 查历史块」指引整句已删。"""
        src = Path(nsm.__file__).read_text(encoding="utf-8")
        assert "可用 read_history_block" not in src

    def test_fold_tool_output_source_never_names_the_tool(self):
        """fold_tool_output 函数体（报错文案来源）内不得出现 read_history_block。"""
        import inspect

        src = inspect.getsource(fold_tool_output)
        assert "read_history_block" not in src

    def test_schema_description_teaches_current_view_only(self):
        """模型面向描述只教正确用法：当前上下文 + 尚未折叠 + 视野外会被拒绝。"""
        desc = nsm.TOOL_SCHEMAS["fold_tool_output"]["description"]
        assert "当前上下文" in desc
        assert "尚未折叠" in desc and "已折叠的不需再折" in desc
        assert "不会被折叠" in desc
        # 不得写入报错细节/实现机制
        assert "read_history_block" not in desc
        assert "rowid" not in desc and "sqlite" not in desc.lower()

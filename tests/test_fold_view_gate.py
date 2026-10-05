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
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
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


def _by_rowid(snapshot: list[tuple]) -> dict[int, tuple]:
    """整表快照 → rowid 索引字典（比对指定行用，避开位置漂移）。"""
    return {r[0]: r for r in snapshot}


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
        assert f"编号 {good}" in text, "成功编号应保留在 message 里"
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
        """模型面向描述只教正确用法：样例教学 + 已折叠的不需再折 + 编号取自当前上下文。"""
        desc = nsm.TOOL_SCHEMAS["fold_tool_output"]["description"]
        oid_desc = nsm.TOOL_SCHEMAS["fold_tool_output"]["input_schema"]["properties"]["output_ids"]["description"]
        assert "它下面跟着这条输出的原文" in desc
        assert "折叠时把其中的编号传给我" in desc
        assert "已经折叠的不需要再折" in desc
        assert "取自当前上下文里 [可折叠编号 N] 中的 N" in oid_desc
        # 不得写入报错细节/实现机制
        assert "read_history_block" not in desc
        assert "rowid" not in desc and "sqlite" not in desc.lower()



# ============== ⑦ 组合：压实后可见集按新窗口重建——显号必可折、窗口外必拒 ==============
# 真 ContextManager + 真 assemble_view_sync 建视野 + 真 compaction.build_compact_view
# 走压实路径 + 真 fold_tool_output 消费；禁真实 LLM/DB（块库、校准、messages.db
# 全部 tmp 注入），agent_loop 只桩提炼/总结/三件套落库/进度通知——机械压实与
# 可见集重建跑真逻辑。不变量：压实后视图里出现的每个可折叠编号都可折
# （旧视图的集合是脏点，新视图的集合按窗口重算写入）。

import agent.context_assembler.calibration as calibration  # noqa: E402
import agent.context_assembler.compaction as compaction_mod  # noqa: E402
import agent.context_manager as cm_mod  # noqa: E402
import agent.session as session_mod  # noqa: E402
from agent.context_assembler.compaction import AUTO_GATE  # noqa: E402
from agent.context_manager import ContextManager  # noqa: E402
from agent.generic import agent_loop  # noqa: E402
from agent.session import Message  # noqa: E402


class _FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


def _fake_count_tokens(messages):
    return sum(len(m.get("content", "")) + 8 for m in messages)


def _msg(idx, role, content, tool_calls=None, tool_call_id="",
         folded=0, output_pct=None):
    return Message(
        id=f"m{idx:03d}", role=role, content=content,
        tool_calls=tool_calls or [], tool_call_id=tool_call_id,
        folded=folded, output_pct=output_pct,
        created_at="2026-10-05T10:00:00", rowid=idx + 1,
    )


def _tc(name, tc_id, args="{}"):
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": args}}


def _long_conversation():
    """五个会话单元：Q1..Q4（各含 1 个工具输出）+ 末单元（Q5 + assistant/tool）。

    工具行 rowid = {3, 6, 9, 12, 15}。keep_recent_turns=1 的压实只保留**末单元**
    （Q5 → assistant → tool rowid 15）——Q1..Q4 的工具行归档出窗口，新窗口内唯一的
    工具行 rowid 15 压实后仍在视图里带 `[可折叠编号 15 · …]` 头行（模型看得见）。
    """
    msgs = []
    for i, q in enumerate(("Q1", "Q2", "Q3", "Q4")):
        base = i * 3
        msgs += [
            _msg(base, "user", q),
            _msg(base + 1, "assistant", "",
                 tool_calls=[_tc("read_file", f"c{i}", '{"path": "x"}')]),
            _msg(base + 2, "tool", f"RAW_BODY_{i}", tool_call_id=f"c{i}",
                 output_pct=5.0 + i),
        ]
    # 末单元 = 当前待执行轮（user → assistant+tool）：keep=1 的压实窗口即此单元
    msgs += [
        _msg(12, "user", "Q5"),
        _msg(13, "assistant", "",
             tool_calls=[_tc("grep", "c4", '{"q": "x"}')]),
        _msg(14, "tool", "RAW_BODY_4", tool_call_id="c4", output_pct=2.5),
    ]
    return msgs


@pytest.fixture
def view_env(monkeypatch, tmp_path):
    """真 CM + tmp messages.db：工具行 rowid {3,6,9,12,15} 可折，末单元含工具行。"""
    blocks_db = tmp_path / "blocks.db"
    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0
    monkeypatch.setattr(session_mod, "_fold_columns_available", True)
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(_fake_count_tokens))
    AUTO_GATE.release()
    msgs = _long_conversation()
    cm = ContextManager(_FakeStore(msgs), max_tokens=1_000_000,
                        blocks_db_path=blocks_db)
    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
    monkeypatch.setattr(compaction_mod, "_read_keep_recent_turns", lambda: 1)
    monkeypatch.setattr(compaction_mod, "archive_excluded_units",
                        lambda messages, units, window_start, db_path,
                        collect_entities=True: 0)
    db = tmp_path / "messages.db"
    _make_messages_db(db, [
        {"rowid": m.rowid, "role": m.role, "content": m.content,
         "id": m.id, "folded": m.folded, "output_pct": m.output_pct}
        for m in msgs
    ])
    yield {"cm": cm, "msgs": msgs, "db": db, "blocks_db": blocks_db,
           "kwargs": {"messages_db_path": str(db)}}
    calibration._cached_ratio = old_ratio
    AUTO_GATE.release()


class _FakeCompactionCtx:
    """压缩上下文鸭子对象——_sync_get_messages 返回测试 db_messages。"""

    def __init__(self, db_messages, blocks_db=None):
        self._db_messages = db_messages
        self._last_compacted_view = None
        self._blocks_db_path = blocks_db  # tmp 块库（禁碰 ~/.niu）

    def _sync_get_messages(self, limit=None):
        return list(self._db_messages)

    def _sync_add_message(self, *args, **kwargs):
        return "msg-x"

    _llm_config = None
    _store = None
    _system_msg = None


def _run_real_compression(db_messages, messages, rebuild_raises: bool = False,
                          blocks_db=None):
    """走真 run_controlled_compression（真机械压实 + 真重建可见集 + 真重组）；
    只桩提炼/总结/三件套落库/进度通知（禁真实 LLM 与真实落库）。

    rebuild_raises=True → 桩掉重组（抛异常）走降级分支，两条路径共用同一合流点。
    """
    ctx = _FakeCompactionCtx(db_messages, blocks_db=blocks_db)
    rebuild = patch.object(agent_loop, "_rebuild_messages_after_compact",
                           side_effect=RuntimeError("rebuild boom")) \
        if rebuild_raises else patch.object(agent_loop,
                                            "_rebuild_messages_after_compact",
                                            wraps=agent_loop._rebuild_messages_after_compact)
    with patch.object(agent_loop, "_extract_f1_before_compress", return_value=True), \
         patch.object(agent_loop, "_extract_cooldown_active", return_value=False), \
         patch.object(agent_loop, "_run_summary_llm", return_value="总结"), \
         patch.object(agent_loop, "_persist_compression_triplet"), \
         patch.object(agent_loop, "_notify_compact_progress"), \
         rebuild:
        new_msgs, did = agent_loop.run_controlled_compression(
            list(messages), ctx, SimpleNamespace(chat=lambda **k: None), 1)
    return ctx, new_msgs, did


_FOLDABLE_ID_RE = re.compile(r"\[可折叠编号 (\d+)")


class TestViewRebuiltByCompaction:
    def test_compaction_rebuilds_visible_set_to_new_window(self, view_env):
        """压实后可见集 = **新窗口**的工具 rowid 集（不作废为 None）：
        折新窗口内编号必须成功、折旧视图（窗口外）编号必须拒绝且零写库。"""
        cm, msgs = view_env["cm"], view_env["msgs"]
        kwargs = view_env["kwargs"]

        # 组装（exclude_last=False = 入口形态）→ 视野 {3,6,9,12,15}（工具行 rowid）
        cm.assemble_view_sync(msgs, exclude_last=False)
        assert cm.visible_tool_rowids() == {3, 6, 9, 12, 15}
        # 真消费通道读到的就是这份集合（不注入假视野）
        assert nsm._current_visible_tool_rowids() == {3, 6, 9, 12, 15}

        # 真压实（keep=1 单元 → Q1..Q4 归档出窗口）：待发视图 = 组装窗口 + system
        pending = [{"role": "system", "content": "sys"}]
        pending += cm.assemble_view_sync(msgs, exclude_last=True)
        _ctx, new_msgs, did = _run_real_compression(
            msgs, pending, blocks_db=view_env["blocks_db"])
        assert did is True
        assert any(agent_loop._COMPRESSION_DONE_HINT in (m.get("content") or "")
                   for m in new_msgs)  # 真重组走完（完成提示在场）
        # 重建：新窗口 = 末单元（Q5 → assistant → tool 15）→ 集合 {15}
        assert cm.visible_tool_rowids() == {15}

        # 窗口外（旧视图）编号：一律拒绝、零写库
        before = _snapshot(view_env["db"])
        refused = _call(kwargs, [3, 9, 12])
        assert refused == {"status": "error", "error": NO_FOLDABLE_ERROR}
        assert _snapshot(view_env["db"]) == before, "窗口外编号不得写任何行"

        # 窗口内编号：真工具折叠成功
        result = _call(kwargs, [15])
        assert result["status"] == "ok" and result["folded"] == [15]
        assert result["freed_pct"] == 2.5
        assert _by_rowid(_snapshot(view_env["db"]))[15][3] == 1
        if os.environ.get("FOLD_EVIDENCE"):
            print(f"\n[EVIDENCE] 压实后可见集={sorted(cm.visible_tool_rowids() or [])}\n"
                  f"[EVIDENCE] 窗口外 [3,9,12] → {refused}\n"
                  f"[EVIDENCE] 窗口内 [15] → {result}")

    def test_degrade_branch_keeps_new_window_set(self, view_env):
        """降级分支（重组抛异常）消费的也是同一个 `_last_compacted_view`——集合在
        生成点已按新窗口写好，合流不再作废：压实后折窗口内编号照旧成功。"""
        cm, msgs = view_env["cm"], view_env["msgs"]
        cm.assemble_view_sync(msgs, exclude_last=False)
        pending = [{"role": "system", "content": "sys"}]
        pending += cm.assemble_view_sync(msgs, exclude_last=True)
        _ctx, new_msgs, did = _run_real_compression(
            msgs, pending, rebuild_raises=True, blocks_db=view_env["blocks_db"])
        assert did is True  # 降级 ≠ 未压缩
        assert any(agent_loop._COMPRESSION_DONE_HINT in (m.get("content") or "")
                   for m in new_msgs)  # 降级分支手工插完成提示
        assert cm.visible_tool_rowids() == {15}
        got = _call(view_env["kwargs"], [15])
        assert got["status"] == "ok" and got["folded"] == [15]

    def test_compact_db_view_generation_point_rebuilds_set(self, view_env):
        """视图生成点（_compact_db_view）按新窗口重建可见集：真压实跑完该步、
        重组尚未发生时，集合已与新视图一致（不是置 None）——旧视图的集合才是
        脏点，新视图的集合可算。"""
        cm, msgs = view_env["cm"], view_env["msgs"]
        cm.assemble_view_sync(msgs, exclude_last=False)
        assert cm.visible_tool_rowids() == {3, 6, 9, 12, 15}

        ctx = _FakeCompactionCtx(msgs, blocks_db=view_env["blocks_db"])
        ok, stats = agent_loop._compact_db_view(ctx)  # 真机械压实（仅此一步）
        assert ok is True and isinstance(stats, dict)
        assert ctx._last_compacted_view, "压实视图已生成"
        assert cm.visible_tool_rowids() == {15}, \
            "视图生成点未按新窗口重建可见集"

    def test_compaction_path_writes_set_exactly_once(self):
        """源码级锁（新语义）：压实路径只在视图生成点写集合——`_compact_db_view`
        消费 build_compact_view 的第三返回值（窗口 rowid 集）并写入；
        重组成功/降级合流**不再**作废（两分支消费的都是同一个
        `_last_compacted_view`，集合已由生成点按新窗口写好）。"""
        import inspect

        compact_src = inspect.getsource(agent_loop._compact_db_view)
        assert "window_rowids" in compact_src or "rowids" in compact_src, \
            "视图生成点未消费 build_compact_view 回传的窗口 rowid 集"
        assert "set_visible_tool_rowids" in compact_src, \
            "视图生成点未写入新窗口可见集"

        body = inspect.getsource(agent_loop.run_controlled_compression)
        assert "_invalidate_visible_tool_rowids" not in body, \
            "合流点不得再作废（矛盾源头：视图显号却拒折）"

    def test_archived_rowids_refused_after_compaction(self, view_env):
        """压实重建后的集合只含新窗口编号：旧视图（已归档出窗口）的编号即使还在
        DB 里也一律拒绝且零写库；窗口内编号照折（同一轮内折得动）。"""
        cm, msgs = view_env["cm"], view_env["msgs"]
        cm.assemble_view_sync(msgs, exclude_last=False)
        pending = [{"role": "system", "content": "sys"}]
        pending += cm.assemble_view_sync(msgs, exclude_last=True)
        _run_real_compression(msgs, pending, blocks_db=view_env["blocks_db"])
        assert cm.visible_tool_rowids() == {15}  # 新窗口编号集

        # 归档把窗口外的行移出块覆盖范围——模拟其已被移走的陈旧编号
        import sqlite3 as _sq
        conn = _sq.connect(str(view_env["db"]))
        conn.execute("DELETE FROM messages WHERE rowid IN (3, 6)")
        conn.commit()
        conn.close()

        before = _snapshot(view_env["db"])
        result = _call(view_env["kwargs"], [3, 6, 12])  # 全是旧视图编号（窗口外）
        assert result == {"status": "error", "error": NO_FOLDABLE_ERROR}
        assert _snapshot(view_env["db"]) == before
        after = _by_rowid(_snapshot(view_env["db"]))
        before_rows = _by_rowid(before)
        assert after.get(3) is None and before_rows.get(3) is None, "已归档行不在表内"
        assert after.get(6) is None and before_rows.get(6) is None

        # 窗口内编号 15：仍可折（集合与视图同刻一致，无「显号却拒折」窗口）
        ok = _call(view_env["kwargs"], [15])
        assert ok["status"] == "ok" and ok["folded"] == [15]
        assert _by_rowid(_snapshot(view_env["db"]))[15][3] == 1

    def test_view_row_missing_in_db_refused_zero_extra_write(self, view_env):
        """视野集里有、DB 里没有该行（并发归档竞态）→ 拒绝，其余照折、释放量不虚报。"""
        cm, msgs = view_env["cm"], view_env["msgs"]
        cm.assemble_view_sync(msgs, exclude_last=False)
        cm.invalidate_visible_tool_rowids()
        assert cm.visible_tool_rowids() is None

        # 从 tmp messages.db 删掉 rowid 12（DB 行没了、内存视图陈旧——删行竞态）
        import sqlite3 as _sq
        conn = _sq.connect(str(view_env["db"]))
        conn.execute("DELETE FROM messages WHERE rowid=12")
        conn.commit()
        conn.close()

        with visible({12, 15}):  # 陈旧集合：含已被删的 rowid 12
            result = _call(view_env["kwargs"], [12, 15])
        assert result["status"] == "ok"
        assert result["folded"] == [15]
        assert result["errors"] == [aggregate_error(1)]
        assert result["freed_pct"] == 2.5, "被拒行不得贡献释放量"
        after = _by_rowid(_snapshot(view_env["db"]))
        assert after.get(12) is None and after[15][3] == 1

    def test_every_number_in_compacted_view_is_foldable(self, view_env):
        """**不变量锁**：压实后那一轮视图里出现的每个可折叠编号，经真工具调用
        逐个验证都可折——不出现「视图显号但被拒」（用户报障的复现面）；
        集合与视图显号逐一致（不是压实前的仪表盘快照/旧集合）。"""
        cm, msgs = view_env["cm"], view_env["msgs"]
        kwargs = view_env["kwargs"]
        pending = [{"role": "system", "content": "sys"}]
        pending += cm.assemble_view_sync(msgs, exclude_last=True)
        ctx, _new_msgs, did = _run_real_compression(
            msgs, pending, blocks_db=view_env["blocks_db"])
        assert did is True

        # 模型该轮看见的压实视图 → 其中渲染出的全部可折叠编号
        displayed: set[int] = set()
        for m in ctx._last_compacted_view or []:
            for sid in _FOLDABLE_ID_RE.findall(m.get("content") or ""):
                displayed.add(int(sid))
        assert displayed == {15}, "用例前提：压实视图里恰有 1 个可折叠编号"

        # 视图显号 ⇒ 必可折（先折后比集合：RED 时此处直接暴露"显号却拒折"的工具回执）
        for rid in sorted(displayed):
            got = _call(kwargs, [rid])
            assert got["status"] == "ok", f"视图显号 {rid} 却被拒: {got}"
            assert got["folded"] == [rid] or any(
                f"编号 {rid}" in n for n in got.get("notes", [])), got
            assert not got["errors"], got

        assert cm.visible_tool_rowids() == displayed, \
            "可见集与视图显号必须逐一致（否则等于把用户报障复现一轮）"
        if os.environ.get("FOLD_EVIDENCE"):
            print(f"\n[EVIDENCE] 压实视图显号={sorted(displayed)} "
                  f"可见集={sorted(cm.visible_tool_rowids() or [])} "
                  f"逐个真工具折叠回执={got}")

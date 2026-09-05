"""组装器水位线模型验收：get_context_for_chat 尊重块库水位线。

背景（双重表示缺陷修复）：/compact 归档后，组装不得把已归档内容装回窗口。
新语义：候选消息 = DB 全量中未被任何块覆盖的尾部消息；视图 = [索引（仅当有块）]
+ [候选原文]；不做预算装填、不在组装路径归档。

覆盖：
1. 无块全新：全量进视图、无索引消息（现状不变）
2. 核心回归钉：归档后组装不再含已归档消息；块数不因组装增长
3. 候选空 → 仅索引
4. 水位线单调推进：压实→组装→再压实，覆盖集合严格增长且每轮组装不含已归档 msg_id
5. 端到端序列：大 DB→compact→视图骤减→追加消息→组装仍不含→再次 compact 正常
6. 前缀稳定：追加消息后视图以先视图为前缀（prompt cache 守卫）

mock store / 校准倍率 / token 计数，禁真实 LLM。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.context_assembler.calibration as calibration
from agent.context_assembler.blocks import PointerBlock, load_all, upsert_blocks
from agent.context_assembler.compaction import AUTO_GATE, build_compact_view
from agent.context_manager import ContextManager
from agent.session import Message


# ---------------------------------------------------------------------------
# 测试基建：FakeStore + 确定性 token 计数 + 校准/闸门隔离
# ---------------------------------------------------------------------------

class FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages: list[Message]):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


def _fake_count_tokens(messages):
    """确定性计数：每条消息 = len(content) + 8 结构开销。

    单参数签名（staticmethod 约定）：类访问与实例访问均以
    count_tokens_simple(messages) 形式命中。
    """
    return sum(len(m.get("content", "")) + 8 for m in messages)


def _msg(idx, role, content, created="2026-08-12T10:00:00"):
    return Message(
        id=f"m{idx:03d}",
        role=role,
        content=content,
        tool_calls=[],
        tool_call_id="",
        created_at=created,
        rowid=idx + 1,
    )


def _unit(start_idx, q, a, created_q="2026-08-12T10:00:00",
          created_a="2026-08-12T10:01:00"):
    """一个最小会话单元：user → assistant。"""
    return [
        _msg(start_idx, "user", q, created_q),
        _msg(start_idx + 1, "assistant", a, created_a),
    ]


@pytest.fixture
def isolated_calibration(monkeypatch):
    """倍率固定 1.0 + AUTO_GATE 复位 + 实体标签 mock——隔离真实持久化状态、
    跨用例闩锁与真实知识图谱访问（archive_excluded_units 的标签反查降级为空）。"""
    import agent.context_assembler.entity_tags as entity_tags

    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0
    AUTO_GATE.release()
    monkeypatch.setattr(entity_tags, "collect_tags",
                        lambda time_ranges, first_users=None: [[] for _ in time_ranges])
    yield
    calibration._cached_ratio = old_ratio
    AUTO_GATE.release()


@pytest.fixture
def cm_factory(tmp_path, monkeypatch):
    """构造注入了临时块 DB 与确定性 token 计数的 ContextManager 工厂。

    staticmethod 包装：build_compact_view 以类访问
    ContextManager.count_tokens_simple(window)，实例路径 self.count_tokens_simple(...)
    两种约定都须命中（test_calibration.py staticmethod 先例）。
    """
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(_fake_count_tokens))

    def _make(store, max_tokens=1_000_000):
        # max_tokens 取极大值：组装出口 80% 触发检查不误触发，
        # 压实由用例显式调 build_compact_view 驱动
        return ContextManager(
            store,
            max_tokens=max_tokens,
            blocks_db_path=tmp_path / "context_blocks.db",
        )

    return _make


def _n_units(n, body_chars=30, start_rowid=0):
    """n 个 plain 会话单元，rowid 从 start_rowid+1 连续编号，返回 (messages, 下一 idx)。

    单元编号取全局位置（idx//2）——追加批次不得与既有单元重名，
    否则视图断言无法区分已归档消息与新消息。
    """
    msgs = []
    idx = start_rowid
    for _ in range(n):
        u = idx // 2
        created = f"2026-08-{12 + u // 20:02d}T10:00:00"
        msgs.extend(_unit(idx, f"Q{u} " + "q" * body_chars, f"A{u} " + "a" * body_chars,
                          created, created))
        idx += 2
    return msgs, idx


def _view_msg_ids(view):
    """从视图窗口消息提取可辨识的消息编号集合（内容首词 Q<N>/A<N>）。

    索引消息跳过——其「首问」字段合法引用已归档首问，不属于窗口原文。
    """
    ids = set()
    for m in view:
        c = m.get("content", "")
        if "[历史索引]" in c:
            continue
        first = c.split()[0] if c.split() else ""
        if len(first) >= 2 and first[0] in ("Q", "A") and first[1:].isdigit():
            ids.add(first)
    return ids


# ---------------------------------------------------------------------------
# 1. 无块全新：现状不变
# ---------------------------------------------------------------------------

class TestFreshNoBlocks:
    async def test_full_history_no_index(self, cm_factory, isolated_calibration):
        msgs = [
            _msg(0, "user", "你好"),
            _msg(1, "assistant", "你好！有什么可以帮你？"),
            _msg(2, "user", "今天天气如何"),
            _msg(3, "assistant", "今天晴天。"),
        ]
        cm = cm_factory(FakeStore(msgs))

        view = await cm.get_context_for_chat(exclude_last=True)

        assert view == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你？"},
            {"role": "user", "content": "今天天气如何"},
        ]

    async def test_fresh_produces_no_blocks_and_no_index(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        msgs, _ = _n_units(2)
        cm = cm_factory(FakeStore(msgs))

        view = await cm.get_context_for_chat(exclude_last=False)

        assert len(view) == 4  # 全量在视图内
        assert all("[历史索引]" not in m["content"] for m in view)
        assert load_all(tmp_path / "context_blocks.db") == []  # 组装路径不产块

    async def test_tool_fields_preserved_in_view(self, cm_factory, isolated_calibration):
        msgs = [
            _msg(0, "user", "查一下"),
            _msg(1, "assistant", ""),
            _msg(2, "tool", '{"r": 1}'),
            _msg(3, "assistant", "结果是 1"),
        ]
        msgs[1].tool_calls = [{"id": "c1", "type": "function",
                               "function": {"name": "t", "arguments": "{}"}}]
        msgs[2].tool_call_id = "c1"
        cm = cm_factory(FakeStore(msgs))

        view = await cm.get_context_for_chat(exclude_last=False)

        assert view[1]["tool_calls"][0]["id"] == "c1"
        assert view[2]["tool_call_id"] == "c1"


# ---------------------------------------------------------------------------
# 2. 核心回归钉：归档后组装不再含已归档消息
# ---------------------------------------------------------------------------

class TestWatermarkAssembly:
    async def test_archived_messages_never_reassembled(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        db = tmp_path / "context_blocks.db"
        msgs, next_idx = _n_units(5)  # 5 个单元
        store = FakeStore(msgs)

        # /compact：留最近 2 轮 → 前 3 个单元归档为块
        _, stats = build_compact_view(
            msgs, system_msg=None, keep_turns=2,
            blocks_db_path=db, context_window_tokens=5000,
        )
        assert stats["blocks_archived"] == 3
        archived_ids = {f"Q{i}" for i in range(3)} | {f"A{i}" for i in range(3)}

        cm = cm_factory(store)
        view = await cm.get_context_for_chat(exclude_last=False)

        # 核心回归钉：视图中不得出现任何已归档消息
        got = _view_msg_ids(view)
        assert not (got & archived_ids), f"已归档消息被重新装回窗口: {got & archived_ids}"

        # 视图 = 索引 + 最近 2 个单元原文
        window = [m for m in view if "[历史索引]" not in m["content"]]
        assert len(window) == 4
        assert window[0]["role"] == "user"
        assert window[0]["content"].startswith("Q3 ")

        # 组装不新增块、不改块表（水位线只读）
        assert len(load_all(db)) == 3

    async def test_repeated_assembly_stable(self, cm_factory, tmp_path, isolated_calibration):
        db = tmp_path / "context_blocks.db"
        msgs, _ = _n_units(4)
        store = FakeStore(msgs)
        build_compact_view(msgs, keep_turns=1, blocks_db_path=db,
                           context_window_tokens=5000)

        cm = cm_factory(store)
        first = await cm.get_context_for_chat(exclude_last=False)
        second = await cm.get_context_for_chat(exclude_last=False)

        assert first == second
        assert len(load_all(db)) == 3  # 幂等：组装不翻倍块数

    async def test_exclude_last_drops_only_tail_candidate(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        db = tmp_path / "context_blocks.db"
        msgs, _ = _n_units(3)
        build_compact_view(msgs, keep_turns=1, blocks_db_path=db,
                           context_window_tokens=5000)

        cm = cm_factory(FakeStore(msgs))
        view_all = await cm.get_context_for_chat(exclude_last=False)
        view_cut = await cm.get_context_for_chat(exclude_last=True)

        assert view_cut == view_all[:-1]

    async def test_discontinuous_candidates_warn_and_take_tail(
        self, cm_factory, tmp_path, isolated_calibration, monkeypatch
    ):
        """意外不连续（append-only 被破坏）：取最后一个被覆盖消息之后的尾部并告警。"""
        warnings: list[str] = []
        monkeypatch.setattr(
            "agent.context_manager.logger",
            type("L", (), {"warning": staticmethod(lambda m, *a, **k: warnings.append(m)),
                           "info": staticmethod(lambda *a, **k: None),
                           "debug": staticmethod(lambda *a, **k: None)}),
        )
        db = tmp_path / "context_blocks.db"
        msgs = []
        idx = 0
        for i in range(4):
            msgs.extend(_unit(idx, f"Q{i}", f"A{i}"))
            idx += 2
        # 只归档第 2 个单元（rowid 3..6），单元 0（rowid 1..2）留在已覆盖
        # 前缀之前——制造「未覆盖—覆盖—尾部」的意外不连续形态
        upsert_blocks([PointerBlock(
            id=1, start_msg_id="m002", end_msg_id="m005",
            start_rowid=3, end_rowid=6, count=4,
        )], db)

        cm = cm_factory(FakeStore(msgs))
        view = await cm.get_context_for_chat(exclude_last=False)

        window = [m for m in view if "[历史索引]" not in m["content"]]
        contents = [m["content"] for m in window]
        # 最后被覆盖消息是 m005（下标 5）→ 候选 = 下标 6..7（Q3、A3）
        assert [c.split()[0] for c in contents] == ["Q3", "A3"]
        # 单元 0 被跳过并告警
        assert any("append-only" in w and "2 条" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# 3. 边界：候选空 → 仅索引
# ---------------------------------------------------------------------------

class TestEmptyCandidates:
    async def test_all_covered_yields_index_only(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        db = tmp_path / "context_blocks.db"
        msgs = []
        idx = 0
        for i in range(3):
            msgs.extend(_unit(idx, f"Q{i}", f"A{i}"))
            idx += 2
        upsert_blocks([PointerBlock(
            id=1, start_msg_id="m000", end_msg_id="m005",
            start_rowid=1, end_rowid=6, count=6,
        )], db)

        cm = cm_factory(FakeStore(msgs))
        view = await cm.get_context_for_chat(exclude_last=False)

        assert len(view) == 1
        assert view[0]["role"] == "user"
        assert view[0]["content"].startswith("[历史索引]")

    async def test_empty_db_no_blocks(self, cm_factory, isolated_calibration):
        cm = cm_factory(FakeStore([]))
        view = await cm.get_context_for_chat(exclude_last=False)
        assert view == []


# ---------------------------------------------------------------------------
# 4. 水位线单调推进：压实→组装→再压实
# ---------------------------------------------------------------------------

class TestWatermarkMonotonicAdvance:
    async def test_watermark_only_moves_forward_across_rounds(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        db = tmp_path / "context_blocks.db"
        msgs, next_idx = _n_units(4)
        store = FakeStore(msgs)
        cm = cm_factory(store)

        covered: set[int] = set()
        prev_max_end = 0
        for round_no in range(3):
            build_compact_view(store.messages, keep_turns=2, blocks_db_path=db,
                               context_window_tokens=5000)
            blocks = load_all(db)
            max_end = max(b.end_rowid for b in blocks)
            # 水位线单调推进：覆盖区间端点只增不减
            covered |= {f"m{r - 1:03d}" for b in blocks
                        for r in range(b.start_rowid, b.end_rowid + 1)}
            prev_max_end = max_end

            # 每轮组装都不含已归档消息
            view = await cm.get_context_for_chat(exclude_last=False)
            got = _view_msg_ids(view)
            archived_contents = set()
            for b in blocks:
                for r in range(b.start_rowid, b.end_rowid + 1):
                    m = store.messages[r - 1]
                    archived_contents.update(m.content.split()[:1])
            assert not (got & archived_contents), \
                f"round {round_no}: 已归档消息被装回窗口"

            # 追加两个新单元进入下一轮
            more, next_idx = _n_units(2, start_rowid=len(store.messages))
            store.messages.extend(more)


# ---------------------------------------------------------------------------
# 5. 端到端序列：模拟用户场景（大 DB → compact → 断言骤减 → 追加 → 再 compact）
# ---------------------------------------------------------------------------

class TestEndToEndUserScenario:
    async def test_compact_assemble_append_recompact(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        db = tmp_path / "context_blocks.db"
        msgs, next_idx = _n_units(8)
        store = FakeStore(msgs)
        cm = cm_factory(store)

        # ① 大 DB 全量视图 vs /compact 后
        full_size = len(await cm.get_context_for_chat(exclude_last=False))
        compact_view, stats = build_compact_view(
            store.messages, keep_turns=3, blocks_db_path=db,
            context_window_tokens=5000,
        )
        assert stats["blocks_archived"] == 5
        assert len(compact_view) < full_size - 5  # 视图骤减（8 单元 16 条 → 索引+6 条）
        archived_ids = {f"Q{i}" for i in range(5)} | {f"A{i}" for i in range(5)}
        assert not (_view_msg_ids(compact_view) & archived_ids)

        # ② compact 后组装：仍不含已归档消息
        view1 = await cm.get_context_for_chat(exclude_last=False)
        assert not (_view_msg_ids(view1) & archived_ids)
        assert len([m for m in view1 if "[历史索引]" not in m["content"]]) == 6

        # ③ 追加新消息后再组装：仍不含已归档消息，新消息在窗内
        more, next_idx = _n_units(2, start_rowid=len(store.messages))
        store.messages.extend(more)
        view2 = await cm.get_context_for_chat(exclude_last=False)
        assert not (_view_msg_ids(view2) & archived_ids)
        tail_contents = [m["content"] for m in view2
                         if "[历史索引]" not in m["content"]]
        assert any(c.startswith("Q8") for c in tail_contents)

        # ④ 再次 compact 正常：块数增加、统计齐全、视图合法
        view3, stats2 = build_compact_view(
            store.messages, keep_turns=3, blocks_db_path=db,
            context_window_tokens=5000,
        )
        assert stats2["blocks_archived"] == 2  # 新增的两个单元出保留轮
        assert stats2["keep_turns"] == 3
        assert len(load_all(db)) == 7
        assert not (_view_msg_ids(view3) & archived_ids)
        assert not (_view_msg_ids(view3) & {"Q5", "A5", "Q6", "A6"})


# ---------------------------------------------------------------------------
# 6. 前缀稳定（prompt cache 守卫）
# ---------------------------------------------------------------------------

class TestPrefixStability:
    async def test_appended_message_keeps_prefix_after_compaction(
        self, cm_factory, tmp_path, isolated_calibration
    ):
        db = tmp_path / "context_blocks.db"
        msgs, _ = _n_units(4)
        store = FakeStore(msgs)
        build_compact_view(msgs, keep_turns=2, blocks_db_path=db,
                           context_window_tokens=5000)

        cm = cm_factory(store)
        v1 = await cm.get_context_for_chat(exclude_last=False)

        store.messages.append(_msg(len(store.messages), "user", "new question",
                                   "2026-08-25T12:00:00"))
        v2 = await cm.get_context_for_chat(exclude_last=False)

        s1 = json.dumps(v1, ensure_ascii=False)
        s2 = json.dumps(v2, ensure_ascii=False)
        assert s2.startswith(s1[:-1]), "追加消息后视图必须以先视图为前缀"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

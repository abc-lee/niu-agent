"""工程三 Task1：F3 梦境工作集原语单测（tmp_path，零真实依赖）。夹具由 format_message_record 真实输出派生，防手拼漂移。

token 计数一律 mock `mdm._record_local_tokens`（禁真实分词器/LLM/LightRAG、禁写 ~/.niu）：
est = 定价 × ratio；断言只看外部可观察量（F3 文件内容 + BuildResult 字段）。
日志断言用 loguru sink 捕获（md_mirror 用 loguru 而非 stdlib logging，caplog 捕获不到——同 test_remove_outer_timeouts.py 模式）。
"""

import json
import os
import re
import sys

import pytest
from loguru import logger

import agent.md_mirror as mdm
from agent.md_mirror import (
    COMPRESSED_TOOL_BODY,
    BuildResult,
    append_record,
    build_f3_from_f2,
    drop_f2_prefix,
    format_message_record,
    truncate_relay_files,
)

_PLACEHOLDER = COMPRESSED_TOOL_BODY[1]


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（md_mirror 用 loguru 而非 stdlib logging，caplog 捕获不到）。"""
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


def _rec(mid, role, content, **kw):
    """format_message_record 的关键字参数（夹具统一从这里派生）。"""
    return dict(msg_id=mid, created_at="t", role=role, content=content, **kw)


def _fill(p2, recs):
    """按 kwargs 列表追加记录块（正文两行 ⇒ meta+2 行正文+空行 = 每条 4 行）；返回 ids。"""
    for r in recs:
        block = format_message_record(**r)
        assert block is not None
        assert append_record(block, p2)
    return [r["msg_id"] for r in recs]


def _tool_cycle(mid, out_text, tc_id):
    """「assistant(tool_calls) + 配对的 tool 输出」一对（spec §3.5 的尾组单元）。"""
    return [
        _rec(mid, "assistant", "调工具",
             tool_calls=[{"id": tc_id, "type": "function", "function": {"name": "bash", "arguments": "{}"}}]),
        _rec(f"{mid}t", "tool", out_text, tool_call_id=tc_id),
    ]


def _calls(*ids):
    """assistant(tool_calls) 的 tool_calls 数组：一次并行开多个 tool_call_id。"""
    return [{"id": i, "type": "function", "function": {"name": "bash", "arguments": "{}"}} for i in ids]


def _three_turns(p2):
    """三个完整轮（每轮 user+assistant）→ 记录起点 [0,4,8,12,16,20]，轮末 [8,16,24]。"""
    return _fill(p2, [
        _rec("u0", "user", "任务一\n第二行"), _rec("a0", "assistant", "结果一\n第二行"),
        _rec("u1", "user", "任务二\n第二行"), _rec("a1", "assistant", "结果二\n第二行"),
        _rec("u2", "user", "任务三\n第二行"), _rec("a2", "assistant", "结果三\n第二行"),
    ])


def _three_records(p2):
    """三条记录（user/assistant/user）→ 记录终点 [4,8,12]，轮末 [8,12]（末轮在 F2 即完整）。"""
    return _fill(p2, [
        _rec("id0", "user", "内容0\n第二行"),
        _rec("id1", "assistant", "内容1\n第二行"),
        _rec("id2", "user", "内容2\n第二行"),
    ])


#: 与 _three_turns 配套的定价：每轮单轮 est=100（ratio=1.0），累计 [100,200,300]
_FLAT_TABLE = {"u0": 40, "a0": 60, "u1": 40, "a1": 60, "u2": 40, "a2": 60}


def _patch_tokens(monkeypatch, spec):
    """token mock：dict=按 msg_id 定价；callable=直接吃记录原文（压缩用例需随正文变短而变短）。"""
    if callable(spec):
        monkeypatch.setattr(mdm, "_record_local_tokens", spec)
    else:
        def by_msg_id(text):
            return spec.get(json.loads(text.split("\n", 1)[0]).get("msg_id", ""), 1)
        monkeypatch.setattr(mdm, "_record_local_tokens", by_msg_id)


def _text(path):
    return open(path, encoding="utf-8").read()


def _read_ids(path):
    if not os.path.exists(path):
        return []
    return re.findall(r'"msg_id":\s*"([^"]+)"', _text(path))


def _lines_of(path):
    """文件行（剥离 split 尾部伪影），与边界值==read 显示行数约定一致。"""
    text = _text(path)
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _roles_of(path):
    return [m.get("role") for _, m in mdm._records(_lines_of(path))]


def _prefix(lines, n):
    """前 n 行（含行尾换行）的文本——F3 应当逐字节等于它（无残片且未压缩时）。"""
    return "".join(ln + "\n" for ln in lines[:n])


@pytest.fixture
def f3_env(tmp_path, monkeypatch):
    """双命名空间 patch agent.md_mirror 的 F2_PATH/F3_PATH → tmp（F1 由 conftest autouse 隔离）；返回 (p2, p3)。"""
    p2 = str(tmp_path / "f2.md")
    p3 = str(tmp_path / "f3.md")
    monkeypatch.setattr(mdm, "F2_PATH", p2)
    monkeypatch.setattr(mdm, "F3_PATH", p3)
    return p2, p3


class TestRecordLocalTokens:
    def test_delegates_to_token_calculator_count_text(self, monkeypatch):
        """口径唯一：TokenCalculator.get().count_text(原文)——不另起分词器、不只算 content。"""
        seen = []

        class FakeCalc:
            def count_text(self, text):
                seen.append(text)
                return len(text) * 3

        class FakeModule:
            class TokenCalculator:
                @classmethod
                def get(cls):
                    return FakeCalc()

        monkeypatch.setitem(sys.modules, "agent.token_calculator", FakeModule)
        assert mdm._record_local_tokens("abc") == 9
        assert seen == ["abc"]


class TestBuildF3Status:
    def test_missing_or_blank_f2_is_no_complete_turn(self, f3_env):
        p2, p3 = f3_env
        empty = BuildResult("no_complete_turn", 0, 0, 0, 0, False, 0, {}, [], [])
        for content in (None, "", "\n   \n\n"):                # 不存在 / 空文件 / 纯空行
            if content is None:
                assert not os.path.exists(p2)
            else:
                with open(p2, "w", encoding="utf-8") as f:
                    f.write(content)
            assert build_f3_from_f2(1000, 1.0) == empty
            assert _text(p3) == ""                             # 先写空 F3

    def test_unparsable_meta_lines_are_malformed(self, f3_env):
        p2, p3 = f3_env
        with open(p2, "w", encoding="utf-8") as f:
            f.write('{"msg_id":\n垃圾\n\n没有元数据行\n')       # 前缀匹配但解析失败 ⇒ 正文 ⇒ 零记录
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            got = build_f3_from_f2(1000, 1.0)
        finally:
            logger.remove(sink_id)
        assert got.status == "malformed"
        assert (got.f3_lines, got.f2_lines, got.turns, got.est_tokens, got.f3_est_tokens) == (0, 0, 0, 0, 0)
        assert got.msg_id_to_turn_end == {} and got.turn_last_msg_ids == [] and got.f3_turn_ends == []
        assert _text(p3) == ""
        assert any("畸形" in m for m in msgs)

    def test_fragments_without_user_are_no_complete_turn(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, lambda text: len(text))
        _fill(p2, [_rec("frag0", "assistant", "残片一\n第二行"),
                   _rec("frag1", "tool", "残片二", tool_call_id="c1")])
        got = build_f3_from_f2(1000, 1.0)
        assert got.status == "no_complete_turn" and (got.f3_lines, got.f2_lines) == (0, 0)
        assert _text(p3) == ""
        assert _read_ids(p2) == ["frag0", "frag1"]              # F2 不动，等下次

    def test_unreadable_f2_is_malformed_io(self, f3_env):
        p2, p3 = f3_env
        os.makedirs(p2)                                        # 真实 IO 失败：F2 位置是目录
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            got = build_f3_from_f2(1000, 1.0)
        finally:
            logger.remove(sink_id)
        assert got.status == "malformed" and _text(p3) == ""
        assert any("IO 失败" in m for m in msgs)               # 与「畸形」区分

    def test_unwritable_f3_is_malformed_io(self, f3_env):
        p2, p3 = f3_env
        _three_turns(p2)
        os.makedirs(p3)                                        # 初始化空 F3 就失败
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            got = build_f3_from_f2(1000, 1.0)
        finally:
            logger.remove(sink_id)
        assert got.status == "malformed"
        assert any("IO 失败" in m for m in msgs)

    def test_write_failure_after_read_leaves_f3_empty(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, dict(_FLAT_TABLE))
        _three_turns(p2)
        real_open = open
        calls = {"n": 0}

        def flaky_open(path, mode="r", *args, **kwargs):
            if "w" in mode:
                calls["n"] += 1
                if calls["n"] >= 2:                            # 1=初始化空 F3，2=正文写出 → 失败
                    raise OSError("simulated F3 write boom")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(mdm, "open", flaky_open, raising=False)
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            got = build_f3_from_f2(1000, 1.0)
        finally:
            logger.remove(sink_id)
        assert got.status == "malformed" and (got.f3_lines, got.f2_lines) == (0, 0)
        assert calls["n"] >= 2 and _text(p3) == ""             # 失败后 F3 仍为空文件
        assert any("IO 失败" in m for m in msgs)

    def test_illegal_budget_or_ratio_does_not_crash(self, f3_env, monkeypatch):
        p2, _ = f3_env
        _patch_tokens(monkeypatch, dict(_FLAT_TABLE))
        _three_turns(p2)
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            assert build_f3_from_f2(None, 1.0).status == "malformed"
            assert build_f3_from_f2("abc", 1.0).status == "malformed"
            assert build_f3_from_f2(1000, None).f2_lines == 24  # ratio 非法按 1.0 计，仍能出 F3
        finally:
            logger.remove(sink_id)
        assert any("token_budget 非法" in m for m in msgs) and any("ratio 非法" in m for m in msgs)


class TestBuildF3Span:
    def test_single_turn_copied_verbatim(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, {"u0": 10, "a0": 20})
        _fill(p2, [_rec("u0", "user", "任务\n第二行"), _rec("a0", "assistant", "结果\n第二行")])
        lines = _lines_of(p2)
        got = build_f3_from_f2(1000, 1.0)
        assert got.status == "ok" and got.turns == 1 and not got.compressed
        assert (got.f3_lines, got.f2_lines) == (len(lines), len(lines))
        assert _text(p3) == _text(p2)                          # 无残片未压缩 → 逐字节相同
        assert (got.est_tokens, got.f3_est_tokens) == (30, 30)

    def test_fragment_before_first_user_excluded(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, {"frag": 500, "u0": 10, "a0": 10})
        _fill(p2, [_rec("frag", "assistant", "遗留残片\n第二行"),
                   _rec("u0", "user", "任务\n第二行"),
                   _rec("a0", "assistant", "结果\n第二行")])
        lines = _lines_of(p2)
        got = build_f3_from_f2(1000, 1.0)
        assert got.status == "ok" and got.turns == 1
        assert got.f2_lines == len(lines) == 12                # 轮末 = 文件末（残片行也在其内）
        assert got.f3_lines == 8 and _text(p3) == _prefix(lines[4:], 8)   # 残片不进 F3
        assert got.msg_id_to_turn_end == {"u0": 12, "a0": 12}  # 残片不建映射
        assert _read_ids(p2) == ["frag", "u0", "a0"]           # 但 F2 里原样留着

    def test_ha_and_timer_users_also_open_turns(self, f3_env, monkeypatch):
        """谁说的都算 user：HA/定时任务触发的 user 记录同样成界（spec §3.1）。"""
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, {"u0": 10, "a0": 10, "u1": 10})
        _fill(p2, [_rec("u0", "user", "用户提问\n第二行"), _rec("a0", "assistant", "回答\n第二行"),
                   _rec("u1", "user", "HA 订阅推送\n第二行")])
        lines = _lines_of(p2)
        got = build_f3_from_f2(15, 1.0)                        # 单轮 est=20，两局取近 → 取首轮
        assert (got.turns, got.f2_lines) == (1, 8)
        assert _read_ids(p3) == ["u0", "a0"] and _read_ids(p2) == ["u0", "a0", "u1"]
        assert got.f3_turn_ends == [(8, 8)] and len(lines) == 12


class TestSelectTurnEnd:
    """选点五局（plan §3：prev/next 代价比小者胜、相等取 prev、单侧取该侧）。"""

    @pytest.fixture
    def flat(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, dict(_FLAT_TABLE))          # 轮末 [8,16,24]，累计 [100,200,300]
        _three_turns(p2)
        return p2, p3

    def test_only_prev_side_takes_last_fitting_turn(self, flat):
        p2, p3 = flat
        got = build_f3_from_f2(1000, 1.0)                      # 全部装得下
        assert (got.turns, got.f2_lines, got.est_tokens) == (3, 24, 300)
        assert not got.compressed and _text(p3) == _text(p2)

    def test_only_next_side_takes_whole_first_turn(self, flat):
        p2, p3 = flat
        got = build_f3_from_f2(50, 1.0)                        # 连首轮都超 → 整首轮照收
        assert (got.turns, got.f2_lines, got.est_tokens) == (1, 8, 100)
        assert not got.compressed                              # 首轮无 tool_calls → 不压缩
        assert _text(p3) == _prefix(_lines_of(p2), 8)          # 仍不切半轮

    def test_grow_cheaper_wins(self, flat):
        p2, p3 = flat
        got = build_f3_from_f2(290, 1.0)                       # 扩大 10 < 回缩 90
        assert (got.turns, got.f2_lines, got.est_tokens) == (3, 24, 300)
        assert _text(p3) == _text(p2)

    def test_shrink_cheaper_wins(self, flat):
        p2, p3 = flat
        got = build_f3_from_f2(210, 1.0)                       # 回缩 10 < 扩大 90
        assert (got.turns, got.f2_lines, got.est_tokens) == (2, 16, 200)
        assert _text(p3) == _prefix(_lines_of(p2), 16)

    def test_tie_prefers_prev(self, flat):
        p2, p3 = flat
        got = build_f3_from_f2(250, 1.0)                       # 扩大 50 == 回缩 50 → 取 prev
        assert (got.turns, got.f2_lines, got.est_tokens) == (2, 16, 200)

    def test_ratio_scales_est_and_choice(self, flat):
        p2, p3 = flat
        got = build_f3_from_f2(150, 0.5)                       # 同一夹具：×0.5 → 累计 [50,100,150]
        assert (got.turns, got.f2_lines, got.est_tokens) == (3, 24, 150)

    def test_turn_is_never_split(self, flat):
        p2, p3 = flat
        for budget in (50, 120, 190, 210, 260, 1000):
            got = build_f3_from_f2(budget, 1.0)
            assert got.f2_lines in (8, 16, 24), f"budget={budget} 切在非轮末 {got.f2_lines}"
            assert _roles_of(p3)[-1] == "assistant"           # 每轮末条都在 F3 里
            assert _read_ids(p3)[-1] == {8: "a0", 16: "a1", 24: "a2"}[got.f2_lines]


class TestCompressOversizedTurn:
    def test_middle_tool_output_replaced_tail_kept(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, len)                        # 按原文计价：压缩后必变小
        _fill(p2, [_rec("u0", "user", "跑一遍\n第二行")]
              + _tool_cycle("a0", "中间输出第一行\n中间第二行\n中间第三行", "c1")
              + [_rec("a1", "assistant", "中间文字KEEP")]
              + _tool_cycle("a2", "尾组输出" + "T" * 300, "c2"))
        src = _text(p2)
        total = len(_lines_of(p2))
        got = build_f3_from_f2(50, 1.0)                        # 单轮远超预算
        assert got.status == "ok" and got.turns == 1 and got.compressed
        assert got.f2_lines == total and got.f3_lines == total - 2   # 三行正文换成一行占位
        assert got.f3_est_tokens < got.est_tokens
        f3 = _text(p3)
        assert f3.count(_PLACEHOLDER) == 1                     # 只省略中间那条
        assert "中间输出第一行" not in f3 and "中间第三行" not in f3
        assert "尾组输出" in f3 and "T" * 300 in f3            # 最后一次工具调用完整保留
        assert "中间文字KEEP" in f3 and "调工具" in f3          # assistant 正文一律原样
        assert '"msg_id": "a0t"' in f3 and '"tool_call_id": "c1"' in f3   # tool meta 逐字节保留
        assert _roles_of(p3) == ["user", "assistant", "tool", "assistant", "assistant", "tool"]
        assert _text(p2) == src                                # F2 不动

    def test_unpaired_last_tool_still_kept(self, f3_env, monkeypatch):
        """该轮末条是未配对的 tool 记录（中途崩溃形态）→ 无条件原样。"""
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, len)
        _fill(p2, [_rec("u0", "user", "跑一遍\n第二行")]
              + _tool_cycle("m1", "中间输出" + "M" * 300, "c1")
              + [_rec("a1", "assistant", "中间文字KEEP"),
                 _rec("a2", "assistant", "再跑一次",
                      tool_calls=[{"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]),
                 _rec("z9", "tool", "孤儿输出" + "O" * 300, tool_call_id="cX")])
        got = build_f3_from_f2(50, 1.0)
        assert got.compressed
        f3 = _text(p3)
        assert "孤儿输出" in f3 and "O" * 300 in f3
        assert f3.count(_PLACEHOLDER) == 1 and "M" * 300 not in f3

    def test_no_tool_calls_assistant_not_compressed(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, len)
        _fill(p2, [_rec("u0", "user", "任务\n第二行"),
                   _rec("t1", "tool", "工具输出" + "X" * 300, tool_call_id="c1"),
                   _rec("a0", "assistant", "收尾")])
        got = build_f3_from_f2(50, 1.0)
        assert got.status == "ok" and not got.compressed
        assert _text(p3) == _text(p2)                          # A 不存在 → 整轮原样

    def test_only_selected_turn_is_compressed(self, f3_env, monkeypatch):
        """前面的轮不压缩：选中末轮超预算，只省略该轮的中间 tool 输出。"""
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, {"u0": 40, "m0": 20, "m0t": 40,           # 首轮 est=100
                                    "u1": 100, "z1": 100, "z1t": 300,
                                    "a1": 100, "z2": 100, "z2t": 200})      # 末轮 est=900
        _fill(p2, [_rec("u0", "user", "首轮提问\n第二行")]
              + _tool_cycle("m0", "首轮输出MID0", "c9")
              + [_rec("u1", "user", "次轮提问\n第二行")]
              + _tool_cycle("z1", "次轮中间输出MID1", "c1")
              + [_rec("a1", "assistant", "中间文字KEEP")]
              + _tool_cycle("z2", "次轮尾组TAIL1", "c2"))
        total = len(_lines_of(p2))
        got = build_f3_from_f2(560, 1.0)                       # 扩大 440 < 回缩 460 → 取末轮
        assert (got.turns, got.f2_lines, got.est_tokens) == (2, total, 1000)
        assert got.compressed and got.f3_lines == total        # 单行输出换单行占位：行数不变
        f3 = _text(p3)
        assert "首轮输出MID0" in f3                            # 首轮的 tool 输出原样
        assert "次轮中间输出MID1" not in f3 and "TAIL1" in f3
        assert f3.count(_PLACEHOLDER) == 1

    def test_within_budget_turn_is_not_compressed(self, f3_env, monkeypatch):
        """未超预算绝不压缩：末轮有 assistant(tool_calls) 但 est ≤ budget → 逐字节原样。

        判别性：删掉实现里 `turn_ests[chosen] > budget` 这半个条件 → compressed=True，
        与末次 tool_calls 不配对的中间 tool 输出会被换成占位 → 本用例失败。
        """
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, {"u0": 10, "a0": 10, "a0t": 300, "a2": 10, "a2t": 10})
        _fill(p2, [_rec("u0", "user", "提问\n第二行")]
              + _tool_cycle("a0", "中间输出第一行\n中间第二行\n中间第三行", "c1")
              + _tool_cycle("a2", "尾组输出第一行\n尾组第二行", "c2"))
        src = _text(p2)
        total = len(_lines_of(p2))
        got = build_f3_from_f2(400, 1.0)                       # 单轮 est=340 ≤ 预算 400
        assert got.status == "ok" and got.turns == 1
        assert (got.est_tokens, got.f2_lines, got.f3_lines) == (340, total, total)
        assert got.compressed is False and got.f3_est_tokens == 340
        f3 = _text(p3)
        assert f3 == src                                       # 无残片 + 未压缩 ⇒ 逐字节前缀
        assert _PLACEHOLDER not in f3 and "中间输出第一行" in f3 and "尾组输出第一行" in f3
        assert _text(p2) == src                                # F2 不动

    def test_parallel_tail_group_kept_whole(self, f3_env, monkeypatch):
        """并行尾组：末次 assistant(tool_calls) 开 2 个 tool_call → 两条配对 tool 输出都完整保留。

        判别性：实现若只认首个 tool_call_id（或只保留最后一条配对记录），
        第二条 tool 输出会被换成占位 → 本用例失败。
        """
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, len)                        # 按原文计价：压缩后必变小
        _fill(p2, [_rec("u0", "user", "并行跑\n第二行")]
              + _tool_cycle("a0", "中间输出第一行\n中间第二行\n中间第三行", "c1")
              + [_rec("a1", "assistant", "并行调两个工具", tool_calls=_calls("c2", "c3"))]
              + [_rec("a1b", "tool", "并行输出一" + "X" * 300, tool_call_id="c2"),
                 _rec("a1c", "tool", "并行输出二" + "Y" * 300, tool_call_id="c3")])
        total = len(_lines_of(p2))
        got = build_f3_from_f2(50, 1.0)                        # 单轮远超预算 → 压缩
        assert got.status == "ok" and got.turns == 1 and got.compressed
        assert got.f3_lines == total - 2                       # 只有中间那条缩成占位
        assert got.f3_est_tokens < got.est_tokens
        f3 = _text(p3)
        assert f3.count(_PLACEHOLDER) == 1 and "中间输出第一行" not in f3
        assert "并行输出一" in f3 and "X" * 300 in f3          # 尾组第一条（非轮末条）也完整
        assert "并行输出二" in f3 and "Y" * 300 in f3          # 尾组第二条（轮末条）完整
        assert '"tool_call_id": "c2"' in f3 and '"tool_call_id": "c3"' in f3
        assert _roles_of(p3) == ["user", "assistant", "tool", "assistant", "tool", "tool"]


class TestBuildF3IdTables:
    def test_ids_map_to_own_turn_end(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, dict(_FLAT_TABLE))
        _three_turns(p2)
        got = build_f3_from_f2(1000, 1.0)
        assert got.msg_id_to_turn_end == {"u0": 8, "a0": 8, "u1": 16, "a1": 16, "u2": 24, "a2": 24}
        assert got.turn_last_msg_ids == ["a0", "a1", "a2"]     # 每轮自末条向前第一条非空
        assert got.f3_turn_ends == [(8, 8), (16, 16), (24, 24)]

    def test_partial_selection_maps_only_written_ids(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, dict(_FLAT_TABLE))
        _three_turns(p2)
        got = build_f3_from_f2(210, 1.0)                       # 只收前两轮
        assert got.msg_id_to_turn_end == {"u0": 8, "a0": 8, "u1": 16, "a1": 16}
        assert got.turn_last_msg_ids == ["a0", "a1"]
        assert got.f3_turn_ends == [(8, 8), (16, 16)]
        assert got.f2_lines == 16 and got.f3_lines == 16

    def test_multiple_ids_of_same_turn_share_turn_end(self, f3_env, monkeypatch):
        p2, _ = f3_env
        _patch_tokens(monkeypatch, dict(_FLAT_TABLE))
        _three_turns(p2)
        got = build_f3_from_f2(1000, 1.0)
        assert got.msg_id_to_turn_end["u0"] == got.msg_id_to_turn_end["a0"] == 8
        assert all(v in (8, 16, 24) for v in got.msg_id_to_turn_end.values())

    def test_empty_and_duplicate_ids_warn(self, f3_env, monkeypatch):
        p2, p3 = f3_env
        _patch_tokens(monkeypatch, lambda text: len(text))
        _fill(p2, [_rec("u0", "user", "提问一\n第二行"), _rec("", "assistant", "空id回答\n第二行"),
                   _rec("u1", "user", "提问二\n第二行"), _rec("u0", "assistant", "跨轮重复id回答\n第二行")])
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            got = build_f3_from_f2(10 ** 9, 1.0)
        finally:
            logger.remove(sink_id)
        assert got.status == "ok"
        assert got.msg_id_to_turn_end == {"u0": 8, "u1": 16}   # 重复取首个轮末；空 id 不建映射
        assert got.turn_last_msg_ids == ["u0", "u0"]           # 每轮各自自末条向前取第一条非空
        assert any("msg_id 为空" in m for m in msgs) and any("msg_id 重复" in m for m in msgs)
        assert _text(p3) == _text(p2)

    def test_all_empty_ids_turn_contributes_no_last_id(self, f3_env, monkeypatch):
        """某轮全空 id（md_alignment 回填路径，plan R-1b）：无映射、无条目，但轮结构不变。"""
        p2, _ = f3_env
        _patch_tokens(monkeypatch, lambda text: len(text))
        _fill(p2, [_rec("", "user", "提问\n第二行"), _rec("", "assistant", "回答\n第二行")])
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            got = build_f3_from_f2(10 ** 9, 1.0)
        finally:
            logger.remove(sink_id)
        assert got.status == "ok" and got.turns == 1
        assert got.msg_id_to_turn_end == {} and got.turn_last_msg_ids == []
        assert got.f3_turn_ends == [(8, 8)]
        assert sum("msg_id 为空" in m for m in msgs) == 2


class TestDropF2Prefix:
    def test_normal_drop(self, f3_env):
        p2, _ = f3_env
        _three_records(p2)
        lines0 = _lines_of(p2)
        assert drop_f2_prefix(8) == (8, "id1")                 # 末删记录 msg_id
        assert _text(p2) == "".join(ln + "\n" for ln in lines0[8:])   # 剩余==原后缀

    def test_final_turn_of_f2_is_a_drop_point(self, f3_env):
        p2, _ = f3_env
        _three_records(p2)
        assert drop_f2_prefix(12) == (12, "id2")               # F2 末轮即完整轮 → 排空
        assert _text(p2) == ""

    def test_snaps_down_to_preceding_turn_end(self, f3_env):
        p2, _ = f3_env
        _fill(p2, [_rec("id0", "user", "内容0\n第二行"), _rec("id1", "assistant", "内容1\n第二行"),
                   _rec("id2", "user", "内容2\n第二行"), _rec("id3", "assistant", "内容3\n第二行")])
        assert drop_f2_prefix(14) == (8, "id1")                # 14 落在次轮中部 → 向下吸附轮末
        assert _read_ids(p2) == ["id2", "id3"]

    def test_drop_inside_first_turn_refuses_and_leaves_file(self, f3_env):
        p2, _ = f3_env
        _three_records(p2)
        original = _text(p2)
        for n in (1, 4, 6):                                    # 都在首轮内部：吸不到轮末 → 不多删
            assert drop_f2_prefix(n) == (0, ""), f"n={n} 应拒绝"
            assert _text(p2) == original, f"n={n} 不得动文件"

    def test_fragment_cleared_with_first_drop(self, f3_env):
        p2, _ = f3_env
        _fill(p2, [_rec("frag", "assistant", "遗留残片\n第二行"),
                   _rec("u1", "user", "提问\n第二行"), _rec("a2", "assistant", "回答\n第二行")])
        original = _text(p2)
        assert drop_f2_prefix(4) == (0, "")                    # 只到残片末：不是轮末 → 不动
        assert _text(p2) == original
        assert drop_f2_prefix(12) == (12, "a2")                # 残片随首次 drop 一起清掉
        assert _text(p2) == ""

    def test_over_max_lines_rejected(self, f3_env):
        p2, _ = f3_env
        _three_records(p2)
        original = _text(p2)
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            assert drop_f2_prefix(8, max_lines=4) == (0, "")
        finally:
            logger.remove(sink_id)
        assert any("超上限" in m for m in msgs)
        assert _text(p2) == original

    def test_invalid_n_rejected(self, f3_env):
        p2, _ = f3_env
        _three_records(p2)
        original = _text(p2)
        for bad_n in (0, -3, "abc", 999):                      # 0/负数/非 int/超总行数(max_lines=None)
            assert drop_f2_prefix(bad_n) == (0, ""), f"n={bad_n!r} 应拒绝"
            assert _text(p2) == original, f"n={bad_n!r} 不得动文件"

    def test_prefix_without_usable_msg_id_rejected(self, f3_env):
        p2, _ = f3_env
        _fill(p2, [_rec("", "user", "提问\n第二行"), _rec("", "assistant", "回答\n第二行"),
                   _rec("keep", "user", "次轮提问\n第二行")])
        original = _text(p2)
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            assert drop_f2_prefix(8) == (0, "")                # 轮末合法但前缀无 msg_id → 畸形
        finally:
            logger.remove(sink_id)
        assert any("前缀无 msg_id" in m for m in msgs)
        assert _text(p2) == original

    def test_write_failure_restores(self, f3_env, monkeypatch):
        p2, _ = f3_env
        _three_records(p2)
        original = _text(p2)
        real = mdm._write_all
        calls = {"n": 0}

        def flaky(fd, data):
            # 第 1 次调用=重写剩余（中途失败）；第 2 次=恢复写——委托真实 _write_all 落盘
            # （纯 no-op mock 的"成功"不写字节，无法验证恢复；同 test_md_relay.py 在 os.write 层注入的思路）
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("boom")
            return real(fd, data)

        monkeypatch.setattr(mdm, "_write_all", flaky)
        assert drop_f2_prefix(8) == (0, "")
        assert _text(p2) == original                           # F2 原文恢复

    def test_drop_single_record_turn_empties(self, f3_env):
        p2, _ = f3_env
        _fill(p2, [_rec("id0", "user", "内容0\n第二行")])
        lines = _lines_of(p2)
        assert drop_f2_prefix(len(lines)) == (len(lines), "id0")
        assert _text(p2) == ""


class TestTruncateRelayFilesF3:
    def test_no_arg_truncates_all_three(self, f3_env):
        p2, p3 = f3_env
        for p in (mdm.F1_PATH, p2, p3):                        # F1 已由 conftest autouse 指向 tmp
            with open(p, "w", encoding="utf-8") as f:
                f.write("data")
        truncate_relay_files()                                 # 无参调用 → 清空 F1/F2/F3
        assert all(_text(p) == "" for p in (mdm.F1_PATH, p2, p3))

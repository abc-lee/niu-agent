"""睡眠段梦境多轮子循环（token 预算 + 完整轮切片，plan v4.4 §3/§4）+ force 摘腿 + nap 缺席的行为测试。

断言只落可观察量：子 Agent 调用序列 / F1-F2-F3 文件状态 / 游标写入 / 返回状态 / 日志。
全 mock：call_subagent_with_auto_answer、游标读写、is_sleeping、runner、token 预算
（niu_api.compat._read_context_window_tokens，budget = 窗口 × 40%）、计数缝
（TokenCalculator.get().count_text + calibration.get_ratio）——禁真实 LLM、禁图谱写入、
messages.db 零新增。

用例清单（plan v4.4 §4/§6.4）：
1. 睡眠成功路径（报末轮末条 → 排空 F2 + 零游标写入）
2. 多轮排空（小预算每轮只装 1 轮，逐轮删到 F2 空）
3. 部分上报留置尾轮（只报首轮末 → 只删到该轮末，尾轮留 F2；零进度才退出）
4. 零进度守卫（无 processed_msg_id → 删 0 → break 防空转）
5. 循环中唤醒（is_sleeping 轮间翻转 → interrupted，已删不回滚）
6. dream 游标退役零写入（成功/fresh 失配/failure 三场景均不写——工程五七件套）
7. F2 空 D5 短路（dream 不调用）
8. 畸形 F2（零记录边界）error 日志
9. failure/incomplete 不动（F2 字节不变）
10. overflow ⌊f3_lines/3⌋ 向下吸附轮末后 break（含无候选 → 删 0）
11. 上报未知 id / 半轮 id / 缺标记 → 删 0 单轮终止
12. clear 后三文件全空
13. nap 入口不复存在
"""
import asyncio
import inspect
import json
from contextlib import ExitStack
from unittest import mock

import pytest

import agent.md_mirror as mdm
import niu_api.compat as compat

NORMAL_JSON = json.dumps({"ok": True})
OVERFLOW_JSON = json.dumps({
    "overflow": True, "agent": "a", "turns_completed": 1,
    "tokens_used": 1, "tokens_limit": 2, "partial_result": "",
})
INCOMPLETE_JSON = json.dumps({
    "incomplete": True, "agent": "a", "reason": "STOPPED", "partial_result": "",
})

MSG_IDS = ["m1", "m2", "m3"]
#: _FakeCalc 逐记录定价（× ratio=1.0）⇒ 一条记录 est=100；预算由此换算
TOKENS_PER_RECORD = 100


class _FakeCalc:
    """计数缝：主 Agent 口径 count_message_single；build 口径 count_text（逐记录原文）。"""

    def count_message_single(self, role, content, tool_calls=None):
        return 100

    def count_text(self, text):
        return TOKENS_PER_RECORD


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0
        self._ensure_session_chain_calls = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        self._ensure_session_chain_calls += 1


def _messages():
    msgs = []
    for mid in MSG_IDS:
        m = mock.MagicMock()
        m.id = mid
        m.role = "user"
        m.content = f"hello {mid}"
        m.tool_calls = None
        m.tool_call_id = None
        msgs.append(m)
    return msgs


def _record(msg_id, role="user"):
    return mdm.format_message_record(
        msg_id=msg_id, created_at="t", role=role, content=f"种子{msg_id}",
    )


def _turn_last_msg_ids_of(path):
    """文件里「每轮自末条向前第一条非空 msg_id」——轮边界取生产 mdm.all_turn_ends，id 取 mdm._msg_id_of。

    dream mock 用它取真实存在的完整轮末上报——禁静态 id（种子一变就假绿/假红）；
    轮规则只在 md_mirror 里有一份，测试不复刻。F3 由 build_f3 从首个 user 记录起写，
    故 [0, 首个轮末) 窗口内不存在前置残片。
    """
    lines = open(path, encoding="utf-8").read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    recs = mdm._records(lines)
    ends = mdm.all_turn_ends(lines)
    out = []
    for lo, end in zip([0] + ends, ends, strict=False):
        ids = [mdm._msg_id_of(meta) for start, meta in recs if lo <= start < end]
        ids = [mid for mid in ids if mid]
        if ids:
            out.append(ids[-1])
    return out


def _dream_report(which=-1):
    """dream mock 工厂：从当前 F3 实际内容取完整轮末 msg_id 上报（which 为轮下标，-1=末轮）。"""
    def _keyed(agent_name=None, **kwargs):
        if agent_name != "dream-evolver":
            return NORMAL_JSON
        ids = _turn_last_msg_ids_of(mdm.F3_PATH)
        assert ids, f"F3 无完整轮末，种子不成立: {mdm.F3_PATH}"
        return NORMAL_JSON + f"\n处理完成 @end\nprocessed_msg_id={ids[which]}"
    return _keyed


def _window_for(token_budget):
    """token 预算 → 窗口：生产里 budget = int(窗口 × 0.4)，测试反向换算窗口。"""
    return int(token_budget / 0.4)


def _patches(call_mock, store_msgs=None, is_sleeping=lambda: True, token_budget=None):
    """公共 patch 组：环境隔离 + 依赖 mock。返回 (store, patch 列表)。

    预算缝：patch _read_context_window_tokens（budget = 窗口 × 40%）；
    计数缝：patch TokenCalculator.get + calibration.get_ratio（进程级缓存，必须显式钉死）。
    门控已随工程四重排摘除（决策 2）——无 _read_cursor_value patch 缝。
    """
    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=store_msgs if store_msgs is not None else _messages())
    window = _window_for(token_budget) if token_budget is not None else 8000
    plist = [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("agent.context_assembler.calibration.get_ratio", return_value=1.0),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=window),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        mock.patch("niu_api.compat.is_sleeping", side_effect=is_sleeping),
        # 游标文件读取全部短路（缺失 → 游标留空）；写入走 mock
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("pathlib.Path.read_text", return_value=""),
    ]
    return store, plist


def _run_sleep(call_mock, seed_ids=(), *, is_sleeping=lambda: True,
               token_budget=None, seed_f2_directly=True, extra_patches=()):
    """驱动 _tidy_context_impl sleep 分支。

    seed_f2_directly：直接向隔离 F2 写种子记录（绕开 entity 步，聚焦梦境循环契约）。
    seed_ids：元素可为 (msg_id, role) 元组或纯 msg_id（role 默认 user）。
    token_budget：单轮工作集预算（内部换算成窗口）。
    返回 (result, write_mock, call_mock, runner, paths)；paths 含 f1/f2/f3。
    """
    from niu_api.compat import _tidy_context_impl

    with ExitStack() as stack:
        store, plist = _patches(call_mock, is_sleeping=is_sleeping,
                                token_budget=token_budget)
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in plist:
            stack.enter_context(p)
        for p in extra_patches:
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock", create=True))
        runner = _FakeRunner()
        stack.enter_context(mock.patch("niu_api.chat.get_or_create_runner", return_value=runner))
        if seed_f2_directly:
            with open(mdm.F2_PATH, "a", encoding="utf-8") as f:
                for sid in seed_ids:
                    mid, role = sid if isinstance(sid, tuple) else (sid, "user")
                    f.write(_record(mid, role))
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    paths = {"f1": mdm.F1_PATH, "f2": mdm.F2_PATH, "f3": mdm.F3_PATH}
    return result, write_mock, call_mock, runner, paths


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def _cursor_writes(write_mock):
    return [call.args[1] for call in write_mock.call_args_list]


def _dream_writes(write_mock):
    return [d for d in _cursor_writes(write_mock) if d.get("last_dream_evolve_id")]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. 睡眠成功路径
# ---------------------------------------------------------------------------

def test_sleep_success_single_round_drains_and_advances():
    """F2 单轮排空：dream 报 F3 末轮末条 msg_id → 删空 F2 → 零游标写入（工程五退役）。"""
    call_mock = mock.MagicMock()
    call_mock.side_effect = _dream_report(-1)
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS,
    )

    assert _called_agents(call_mock) == ["dream-evolver"], (
        f"T6 后 F1 空 entity 跳过、cm 已退役，仅梦境一轮排空: {[_called_agents(call_mock)]}"
    )
    assert _read(paths["f2"]) == "", "报末轮末条后 F2 应已删空"
    writes = _dream_writes(write_mock)
    assert writes == [], "dream 游标已退役（工程五），全程零写入"
    assert result.get("status") == "ok", f"应正常完成: {result}"


# ---------------------------------------------------------------------------
# 2. 多轮排空
# ---------------------------------------------------------------------------

def test_sleep_multi_round_drains():
    """小预算逐轮消化：预算只装得下 1 轮，3 轮积压 → 3 次派发，逐轮删到 F2 空。"""
    call_mock = mock.MagicMock()
    calls = {"dream": 0}

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            calls["dream"] += 1
            ids = _turn_last_msg_ids_of(mdm.F3_PATH)
            assert ids, "F3 无完整轮末，种子不成立"
            return NORMAL_JSON + f"\n处理完成 @end\nprocessed_msg_id={ids[0]}"
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS, token_budget=TOKENS_PER_RECORD,
    )

    assert calls["dream"] == 3, f"3 条积压应恰好 3 轮: {calls['dream']}"
    assert _read(paths["f2"]) == "", "多轮排空后 F2 应为空"
    writes = _dream_writes(write_mock)
    assert writes == [], f"多轮排空零游标写入: {writes}"
    assert result.get("status") == "ok"


# ---------------------------------------------------------------------------
# 3. 部分上报：只删到上报轮末，尾轮留置 F2
# ---------------------------------------------------------------------------

def test_sleep_partial_report_keeps_tail_turns_in_f2():
    """只报首轮末 msg_id（F3 装 3 轮）→ 当轮只删到该轮末，尾轮留 F2；下一轮零进度才退出。

    反证「删到上报轮末会连尾轮一起删」与「不空转」：F2 未删空但循环必须退出。
    """
    call_mock = mock.MagicMock()
    rounds = {"dream": 0}

    def _keyed(agent_name=None, **kwargs):
        if agent_name != "dream-evolver":
            return NORMAL_JSON
        rounds["dream"] += 1
        if rounds["dream"] == 1:
            ids = _turn_last_msg_ids_of(mdm.F3_PATH)
            assert ids, "F3 无完整轮末，种子不成立"
            return NORMAL_JSON + f"\n处理完成 @end\nprocessed_msg_id={ids[0]}"
        return NORMAL_JSON  # 第二轮起不再上报 → 删 0 → break

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS,
    )

    assert rounds["dream"] == 2, f"删 >0 必须续跑，删 0 才退出: {rounds['dream']}"
    rest = _read(paths["f2"])
    assert '"msg_id": "m2"' in rest and '"msg_id": "m3"' in rest, "尾轮两条应留置 F2"
    assert '"msg_id": "m1"' not in rest, "已处理首轮应被删除"
    writes = _dream_writes(write_mock)
    assert writes == [], "部分上报零游标写入"
    assert result.get("status") == "ok"


# ---------------------------------------------------------------------------
# 4. 零进度守卫
# ---------------------------------------------------------------------------

def test_sleep_zero_progress_guard_breaks():
    """无 processed_msg_id → 删 0 → break 防空转（§4「删 0 才退出」）。"""
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        return NORMAL_JSON  # 无 processed_msg_id → 缺失态 → 删除 0

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS, token_budget=TOKENS_PER_RECORD,
    )

    assert _called_agents(call_mock).count("dream-evolver") == 1, "零进度必须当轮 break"
    assert _read(paths["f2"]).count('{"msg_id":') == 3, "F2 原样保留"
    assert _dream_writes(write_mock) == [], "零进度不写游标"


# ---------------------------------------------------------------------------
# 5. 循环中唤醒
# ---------------------------------------------------------------------------

def test_sleep_interrupted_mid_loop():
    """轮间唤醒检查：round1 后仍睡眠继续、round2 后被唤醒 → interrupted；已删不回滚。"""
    # 消费顺序（T7 后，journal 腿迁 scheduler）：CP1(entity 后)(T) → round1 唤醒检查(T) → round2 唤醒检查(False)
    flips = iter([True, True, False])
    call_mock = mock.MagicMock()
    call_mock.side_effect = _dream_report(0)
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS, is_sleeping=lambda: next(flips),
        token_budget=TOKENS_PER_RECORD,
    )

    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock).count("dream-evolver") == 2, "第二轮完成后才打断"
    writes = _dream_writes(write_mock)
    assert writes == [], "已删部分不回滚（零游标写入）"
    rest = _read(paths["f2"])
    assert '"msg_id": "m1"' not in rest and '"msg_id": "m2"' not in rest, "已删部分不回滚"
    assert '"msg_id": "m3"' in rest


# ---------------------------------------------------------------------------
# 6. dream 游标退役零写入（三场景）
# ---------------------------------------------------------------------------

def test_cursor_write_retired_zero_writes_all_states():
    """工程五七件套退役反向钉：成功排空/末删 id 失配/failure 三场景均零 last_dream_evolve 写入。

    旧行为（三态）：成功且 fresh 校验通过才写新 id；退役后 dream 循环无任何游标读写。
    """
    # 态 1：成功排空
    call_ok = mock.MagicMock()
    call_ok.side_effect = _dream_report(-1)
    _, w1, _, _, paths1 = _run_sleep(call_ok, seed_ids=["m1", "m2", "m3"])
    assert _dream_writes(w1) == [], "退役后成功路径也不写 dream 游标"
    assert _read(paths1["f2"]) == "", "F2 删除行为不受退役影响"

    # 态 2：drop 的末删 msg_id 不在 DB（种子用伪 id）
    call_fake = mock.MagicMock()
    call_fake.side_effect = _dream_report(-1)
    _, w2, _, _, _ = _run_sleep(call_fake, seed_ids=["ghost-1", "ghost-2"])
    assert _dream_writes(w2) == []

    # 态 3：failure 前缀 → F2 字节不变
    call_fail = mock.MagicMock()

    def _fail(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return "[错误]同名子 Agent 已在运行（注册冲突）"
        return NORMAL_JSON

    call_fail.side_effect = _fail
    _, w3, _, _, paths = _run_sleep(call_fail, seed_ids=["m1"])
    assert _dream_writes(w3) == []
    assert '"msg_id": "m1"' in _read(paths["f2"]), "failure 时 F2 不动"


# ---------------------------------------------------------------------------
# 7. F2 空 D5 短路
# ---------------------------------------------------------------------------

def test_empty_f2_skips_dream_call():
    """F2 空/不存在 → D5 短路：dream 不调用，流程继续。"""
    call_mock = mock.MagicMock()
    call_mock.return_value = NORMAL_JSON
    result, write_mock, call_mock, runner, _paths = _run_sleep(call_mock)

    assert "dream-evolver" not in _called_agents(call_mock), "F2 空不得调起 dream"
    assert result.get("status") == "skipped" or result.get("status") == "ok"
    assert _dream_writes(write_mock) == []


# ---------------------------------------------------------------------------
# 8. 畸形 F2（零记录边界）error 日志
# ---------------------------------------------------------------------------

def test_malformed_f2_logs_error_and_skips():
    """F2 有内容但零 {"msg_id": 边界 → build malformed → error 日志 + dream 不调用。"""
    call_mock = mock.MagicMock()
    call_mock.return_value = NORMAL_JSON
    err_logs = []

    def _sink(message):
        err_logs.append(str(message))

    from loguru import logger
    sink_id = logger.add(_sink, level="ERROR")
    try:
        result, write_mock, call_mock, _, paths = _run_sleep(
            call_mock, seed_f2_directly=False,
            extra_patches=(),
        )
        # 手动写畸形内容（绕过 _record 的合法形态）
        with open(paths["f2"], "w", encoding="utf-8") as f:
            f.write("垃圾内容，没有元数据行\n")
        # 重新驱动一次（上一跑 F2 为空已短路）
        call_mock2 = mock.MagicMock()
        call_mock2.return_value = NORMAL_JSON
        result, write_mock, call_mock2, _, _ = _run_sleep(call_mock2, seed_f2_directly=False)
    finally:
        logger.remove(sink_id)

    assert "dream-evolver" not in _called_agents(call_mock2), "畸形停摆不得调起 dream"
    assert any("build_f3 malformed" in m for m in err_logs), f"应记 error 日志: {err_logs}"
    assert _dream_writes(write_mock) == []


# ---------------------------------------------------------------------------
# 9. failure/incomplete 不动（F2 字节不变）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_result", ["[错误]注册冲突", INCOMPLETE_JSON])
def test_failure_incomplete_leaves_f2_byte_identical(bad_result):
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return bad_result
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, _, paths = _run_sleep(call_mock, seed_ids=["m1", "m2"])

    assert _read(paths["f2"]).count('{"msg_id":') == 2, "F2 必须字节不变"
    assert _dream_writes(write_mock) == []
    assert _called_agents(call_mock).count("dream-evolver") == 1


# ---------------------------------------------------------------------------
# 10. overflow ⌊f3_lines/3⌋ 向下吸附完整轮末后 break
# ---------------------------------------------------------------------------

def test_overflow_partial_progress_then_break():
    """overflow → ⌊f3_lines/3⌋=3 向下吸附到首个完整轮末（3 行）删 → 零游标写入 → break。"""
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return OVERFLOW_JSON  # f3=9 行 → idx=3 → 候选轮末 {3,6,9} 中 ≤3 的最大=3
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, _, paths = _run_sleep(call_mock, seed_ids=MSG_IDS)

    assert _called_agents(call_mock).count("dream-evolver") == 1, "overflow 当轮 break"
    rest = _read(paths["f2"])
    assert '"msg_id": "m1"' not in rest, "前 ⌊9/3⌋=3 行（首条记录）应已删除"
    assert rest.count('{"msg_id":') == 2
    assert _dream_writes(write_mock) == [], "overflow 部分进度也零游标写入"


def test_overflow_without_turn_end_candidate_deletes_nothing():
    """溢出且 ⌊f3_lines/3⌋ 之前无完整轮末候选（唯一轮末落在 idx 之后）→ 删 0 + WARNING。

    单轮 F3：两条记录 = 6 行 → idx=max(1,6//3)=2 < 6（唯一轮末行）→ candidates 空。
    """
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return OVERFLOW_JSON
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    warn_logs = []
    from loguru import logger
    sink_id = logger.add(lambda m: warn_logs.append(str(m)), level="WARNING")
    try:
        seeded = _record("m1", "user") + _record("m2", "assistant")
        result, write_mock, call_mock, _, paths = _run_sleep(
            call_mock, seed_ids=[("m1", "user"), ("m2", "assistant")])
    finally:
        logger.remove(sink_id)

    assert _called_agents(call_mock).count("dream-evolver") == 1, "溢出当轮 break（不论删多少）"
    assert _read(paths["f2"]) == seeded, "无候选 → 不调 drop，F2 必须字节不变"
    assert any("无完整轮末候选" in m for m in warn_logs), f"应记 WARNING: {warn_logs}"
    assert _dream_writes(write_mock) == [], "溢出零进度不写游标"
    assert result.get("status") == "ok", f"终止分支仍走收尾：{result}"


# ---------------------------------------------------------------------------
# 11. 上报未知 id / 半轮 id / 缺标记 → 删 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reported", [
    "processed_msg_id=ghost-not-in-f3",  # 未知：不在本轮 F3 记录内
    "processed_msg_id=m1",               # 半轮：m1 是 user 首条，不是轮末条
    "完全没有该行",                        # 缺失：无 processed_msg_id
])
def test_unknown_or_half_turn_report_rejected(reported):
    """§2 判序缺失/未知/半轮 → 删 0 不写游标；零进度当轮终止不空转。

    种子是一个完整轮（user m1 + assistant m1a）：轮末条是 m1a，故 m1 落半轮态。
    """
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON + f"\n处理完成 @end\n{reported}"
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, _, paths = _run_sleep(
        call_mock, seed_ids=[("m1", "user"), ("m1a", "assistant")],
    )

    assert _read(paths["f2"]).count('{"msg_id":') == 2, "F2 不动"
    assert _dream_writes(write_mock) == []
    assert _called_agents(call_mock).count("dream-evolver") == 1, "零进度当轮终止"


# ---------------------------------------------------------------------------
# 12. clear 后三文件全空
# ---------------------------------------------------------------------------

def test_clear_truncates_all_three_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mdm, "F1_PATH", str(tmp_path / "f1.md"))
    monkeypatch.setattr(mdm, "F2_PATH", str(tmp_path / "f2.md"))
    monkeypatch.setattr(mdm, "F3_PATH", str(tmp_path / "f3.md"))
    for p in (mdm.F1_PATH, mdm.F2_PATH, mdm.F3_PATH):
        with open(p, "w", encoding="utf-8") as f:
            f.write(_record("x"))

    mdm.truncate_relay_files()

    for p in (mdm.F1_PATH, mdm.F2_PATH, mdm.F3_PATH):
        with open(p, encoding="utf-8") as f:
            assert f.read() == "", f"{p} 应被清空"


# ---------------------------------------------------------------------------
# 13. nap 入口不复存在
# ---------------------------------------------------------------------------

def test_nap_entrypoints_absent():
    from agent.runner import NiuRunner

    assert not hasattr(NiuRunner, "_maybe_trigger_nap")
    assert not hasattr(NiuRunner, "_run_nap_background")
    worker_src = inspect.getsource(compat._pipeline_worker)
    assert "nap" not in worker_src.lower(), "管道 worker 不应再有 nap 分支"

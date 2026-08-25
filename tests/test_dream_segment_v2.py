"""工程三 T4-D：睡眠段梦境多轮子循环（决策 9）+ force 摘腿 + nap 缺席的行为测试。

断言只落可观察量：子 Agent 调用序列 / F1-F2-F3 文件状态 / 游标写入 / 返回状态 / 日志。
全 mock：call_subagent_with_auto_answer、游标读写、is_sleeping、runner、F3 预算
（mdm._f3_max_bytes）——禁真实 LLM、禁图谱写入、messages.db 零新增。

用例清单（计划 T4-D）：
1. 睡眠成功路径（单轮 covered_all 排空 + 零游标写入 + 压缩执行）
2. 多轮排空（小预算逐记录消化，covered_all 终止）
3. 留置尾部终止（M<F3 末行时 F2 留置尾部，不空转）
4. 零进度守卫（covered_all=False 且零删除 → break 防空转）
5. 循环中唤醒（is_sleeping 轮间翻转 → interrupted，已删不回滚）
6. dream 游标退役零写入（成功/fresh 失配/failure-incomplete 三场景均不写——工程五七件套）
7. mode-1 压缩切片无上界（end_cursor 契约作废——工程四 3'a 移除，模式一全量切片）
8. F2 空 D5 短路（dream 不调用）
9. 畸形 F2（零记录边界）error 日志
10. failure/incomplete 不动（F2 字节不变）
11. overflow ⌊f3_lines/3⌋ 部分进度后 break
12. M 无效 / 超 f3_lines 拒绝
13. force 不再调 dream-evolver 且门控放行
14. clear 后三文件全空
15. nap 入口不复存在
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


class _FakeCalc:
    def count_message_single(self, role, content, tool_calls=None):
        return 100


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


def _record(msg_id):
    return mdm.format_message_record(
        msg_id=msg_id, created_at="t", role="user", content=f"种子{msg_id}",
    )


def _patches(call_mock, store_msgs=None, is_sleeping=lambda: True,
             f3_budget=None):
    """公共 patch 组：环境隔离 + 依赖 mock。返回 (patch 列表, write_mock 占位)。

    门控已随工程四重排摘除（决策 2）——无 _read_cursor_value patch 缝。
    """
    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=store_msgs if store_msgs is not None else _messages())
    plist = [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
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
    if f3_budget is not None:
        plist.append(mock.patch.object(mdm, "_f3_max_bytes", lambda: f3_budget))
    return store, plist


def _run_sleep(call_mock, seed_ids=(), *, is_sleeping=lambda: True,
               f3_budget=None, seed_f2_directly=True, extra_patches=()):
    """驱动 _tidy_context_impl sleep 分支。

    seed_f2_directly：直接向隔离 F2 写种子记录（绕开 entity 步，聚焦梦境循环契约）。
    返回 (result, write_mock, call_mock, runner, paths)；paths 含 f1/f2/f3。
    """
    from niu_api.compat import _tidy_context_impl

    with ExitStack() as stack:
        store, plist = _patches(call_mock, is_sleeping=is_sleeping,
                                f3_budget=f3_budget)
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in plist:
            stack.enter_context(p)
        for p in extra_patches:
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
        runner = _FakeRunner()
        stack.enter_context(mock.patch("niu_api.chat.get_or_create_runner", return_value=runner))
        if seed_f2_directly:
            with open(mdm.F2_PATH, "a", encoding="utf-8") as f:
                for sid in seed_ids:
                    f.write(_record(sid))
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    paths = {"f1": mdm.F1_PATH, "f2": mdm.F2_PATH, "f3": mdm.F3_PATH}
    return result, write_mock, call_mock, runner, paths


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def _cursor_writes(write_mock):
    return [call.args[1] for call in write_mock.call_args_list]


def _dream_writes(write_mock):
    return [d for d in _cursor_writes(write_mock) if d.get("last_dream_evolve_id")]


# ---------------------------------------------------------------------------
# 1. 睡眠成功路径
# ---------------------------------------------------------------------------

def test_sleep_success_single_round_drains_and_advances():
    """F2 单轮排空：dream 报全量行号 → 删空 F2 → 零游标写入（工程五退役）→ 压缩执行 。"""
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=9"  # 3 记录共 9 行全删
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS,
    )

    assert _called_agents(call_mock) == ["dream-evolver"], (
        f"T6 后 F1 空 entity 跳过、cm 已退役，仅梦境一轮排空: {[_called_agents(call_mock)]}"
    )
    with open(paths["f2"], encoding="utf-8") as f:
        assert f.read() == "", "covered_all 全删后 F2 应为空"
    writes = _dream_writes(write_mock)
    assert writes == [], "dream 游标已退役（工程五），全程零写入"
    assert result.get("status") == "ok", f"应正常完成: {result}"


# ---------------------------------------------------------------------------
# 2. 多轮排空
# ---------------------------------------------------------------------------

def test_sleep_multi_round_drains():
    """小预算逐记录消化：每轮 f3 只装 1 条记录，3 轮后 covered_all 终止。"""
    budget = len(_record("m1").encode("utf-8"))  # 仅首条记录边界可达
    call_mock = mock.MagicMock()
    calls = {"dream": 0}

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            calls["dream"] += 1
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=3"  # 每轮删当前首条
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS, f3_budget=budget,
    )

    assert calls["dream"] == 3, f"3 条积压应恰好 3 轮: {calls['dream']}"
    with open(paths["f2"], encoding="utf-8") as f:
        assert f.read() == "", "多轮排空后 F2 应为空"
    writes = _dream_writes(write_mock)
    assert writes == [], f"多轮排空零游标写入: {writes}"
    assert result.get("status") == "ok"


# ---------------------------------------------------------------------------
# 3. 留置尾部终止
# ---------------------------------------------------------------------------

def test_sleep_leftover_tail_terminates_on_covered_all():
    """Agent 合法留置尾部（只报首条）：covered_all=True 本轮即终止，尾部留待下轮。

    反证「按 F2 删空判会无限重喂」：本轮 F2 未删空但循环必须退出且不空转。
    """
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            # F3 覆盖全量 3 条（budget 放开），Agent 只处理到首条记录末（第 3 行）
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=3"
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS,
    )

    assert _called_agents(call_mock).count("dream-evolver") == 1, "covered_all 一轮即终止"
    with open(paths["f2"], encoding="utf-8") as f:
        rest = f.read()
    assert '"msg_id": "m2"' in rest and '"msg_id": "m3"' in rest, "尾部两条应留置 F2"
    assert '"msg_id": "m1"' not in rest, "已处理首条应被删除"
    writes = _dream_writes(write_mock)
    assert writes == [], "留置尾部零游标写入"
    assert result.get("status") == "ok"


# ---------------------------------------------------------------------------
# 4. 零进度守卫
# ---------------------------------------------------------------------------

def test_sleep_zero_progress_guard_breaks():
    """covered_all=False 且本轮零删除（无有效 processed_line）→ break 防空转。"""
    budget = len(_record("m1").encode("utf-8"))
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON  # 无 processed_line → 解析失败 → 删除 0
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS, f3_budget=budget,
    )

    assert _called_agents(call_mock).count("dream-evolver") == 1, "零进度必须当轮 break"
    with open(paths["f2"], encoding="utf-8") as f:
        assert f.read().count('{"msg_id":') == 3, "F2 原样保留"
    assert _dream_writes(write_mock) == [], "零进度不写游标"


# ---------------------------------------------------------------------------
# 5. 循环中唤醒
# ---------------------------------------------------------------------------

def test_sleep_interrupted_mid_loop():
    """轮间唤醒检查：round1 后仍睡眠继续、round2 后被唤醒 → interrupted；已删不回滚。"""
    budget = len(_record("m1").encode("utf-8"))
    # 消费顺序（T7 后，journal 腿迁 scheduler）：CP1(entity 后)(T) → round1 唤醒检查(T) → round2 唤醒检查(False)
    flips = iter([True, True, False])
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=3"
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, runner, paths = _run_sleep(
        call_mock, seed_ids=MSG_IDS, is_sleeping=lambda: next(flips), f3_budget=budget,
    )

    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock).count("dream-evolver") == 2, "第二轮完成后才打断"
    writes = _dream_writes(write_mock)
    assert writes == [], "已删部分不回滚（零游标写入）"
    with open(paths["f2"], encoding="utf-8") as f:
        rest = f.read()
    assert '"msg_id": "m1"' not in rest and '"msg_id": "m2"' not in rest, "已删部分不回滚"
    assert '"msg_id": "m3"' in rest


# ---------------------------------------------------------------------------
# 6. dream 游标退役零写入（三场景）
# ---------------------------------------------------------------------------

def test_cursor_write_retired_zero_writes_all_states():
    """工程五七件套退役反向钉：成功/末删 id 失配/failure 三场景均零 last_dream_evolve 写入。

    旧行为（三态）：成功且 fresh 校验通过才写新 id；退役后 dream 循环无任何游标读写。
    """
    # 态 1：成功排空
    call_ok = mock.MagicMock()

    def _ok(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=9"
        return NORMAL_JSON

    call_ok.side_effect = _ok
    _, w1, _, _, paths1 = _run_sleep(call_ok, seed_ids=["m1", "m2", "m3"])
    assert _dream_writes(w1) == [], "退役后成功路径也不写 dream 游标"
    with open(paths1["f2"], encoding="utf-8") as f:
        assert f.read() == "", "F2 删除行为不受退役影响"

    # 态 2：drop 的末删 msg_id 不在 DB（种子用伪 id）
    call_fake = mock.MagicMock()

    def _fake(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=9"
        return NORMAL_JSON

    call_fake.side_effect = _fake
    _, w2, _, _, _ = _run_sleep(call_fake, seed_ids=["ghost-1"])
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
    with open(paths["f2"], encoding="utf-8") as f:
        assert '"msg_id": "m1"' in f.read(), "failure 时 F2 不动"


# ---------------------------------------------------------------------------
# 7. mode-1 切片无上界（end_cursor 契约作废——工程四决策 3'a）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8. F2 空 D5 短路
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
# 9. 畸形 F2（零记录边界）error 日志
# ---------------------------------------------------------------------------

def test_malformed_f2_logs_error_and_skips():
    """F2 有内容但零 {"msg_id": 边界 → build 返回 0 → error 日志 + dream 不调用。"""
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
    assert any("build_f3 返回 0" in m for m in err_logs), f"应记 error 日志: {err_logs}"
    assert _dream_writes(write_mock) == []


# ---------------------------------------------------------------------------
# 10. failure/incomplete 不动（F2 字节不变）
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

    with open(paths["f2"], encoding="utf-8") as f:
        content = f.read()
    assert content.count('{"msg_id":') == 2, "F2 必须字节不变"
    assert _dream_writes(write_mock) == []
    assert _called_agents(call_mock).count("dream-evolver") == 1


# ---------------------------------------------------------------------------
# 11. overflow ⌊f3_lines/3⌋ 部分进度后 break
# ---------------------------------------------------------------------------

def test_overflow_partial_progress_then_break():
    """overflow → drop 前 ⌈f3/3⌉ 行（吸附到首条记录边界）→ 零游标写入 → break。"""
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return OVERFLOW_JSON  # f3=9 行 → drop n=3 → 吸附到首记录边界 3 行
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, _, paths = _run_sleep(call_mock, seed_ids=MSG_IDS)

    assert _called_agents(call_mock).count("dream-evolver") == 1, "overflow 当轮 break"
    with open(paths["f2"], encoding="utf-8") as f:
        rest = f.read()
    assert '"msg_id": "m1"' not in rest, "前 ⌊9/3⌋=3 行（首条记录）应已删除"
    assert rest.count('{"msg_id":') == 2
    assert _dream_writes(write_mock) == [], "overflow 部分进度也零游标写入"


# ---------------------------------------------------------------------------
# 12. M 无效 / 超 f3_lines 拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reported", ["processed_line=0", "processed_line=999", "完全没有该行"])
def test_invalid_or_oversized_m_rejected(reported):
    """M 无效或 > f3_lines → drop 拒绝 → 零删除不写游标；covered_all 单轮终止不空转。"""
    call_mock = mock.MagicMock()

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "dream-evolver":
            return NORMAL_JSON + f"\n处理完成 @end\n{reported}"
        return NORMAL_JSON

    call_mock.side_effect = _keyed
    result, write_mock, call_mock, _, paths = _run_sleep(call_mock, seed_ids=["m1"])

    with open(paths["f2"], encoding="utf-8") as f:
        assert f.read().count('{"msg_id":') == 1, "F2 不动"
    assert _dream_writes(write_mock) == []
    assert _called_agents(call_mock).count("dream-evolver") == 1, "零进度+covered_all 单轮终止"


# ---------------------------------------------------------------------------
# 13. force 不再调 dream-evolver 且门控放行
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 14. clear 后三文件全空
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
# 15. nap 入口不复存在
# ---------------------------------------------------------------------------

def test_nap_entrypoints_absent():
    from agent.runner import NiuRunner

    assert not hasattr(NiuRunner, "_maybe_trigger_nap")
    assert not hasattr(NiuRunner, "_run_nap_background")
    worker_src = inspect.getsource(compat._pipeline_worker)
    assert "nap" not in worker_src.lower(), "管道 worker 不应再有 nap 分支"

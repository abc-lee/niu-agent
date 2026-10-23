"""T5 测试：sleep 状态机检查点 CP1-CP3（force/runner-force 零插入）。

设计见 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §4.2 / §5 T5 / §6 T5。
全 mock：call_subagent_with_auto_answer / 游标文件 / is_sleeping / runner——禁真实 LLM、禁图谱写入、messages.db 零新增。

检查点契约（仅 mode=='sleep'；T7 journal 迁 scheduler 后管道序：entity-extractor → dream-evolver → 块摘要可选层）：
- CP0 worker 取出 sleep 任务执行前：非睡眠 → {"status":"cancelled","reason":"woke_up"}，impl 零调用
- CP1 entity 段完成后：非睡眠 → {"status":"interrupted","reason":"woke_up"}，dream 不执行（F1 已剪切不回滚）
- dream 循环内删除前缀后中断检查：同上
- CP2 dream 循环完成后（纯中断检查）：同上
- 已推进游标不回滚（游标保留 CP 打断时的推进值，下次续跑）
- force/runner-force 零插入（反向断言：mock is_sleeping 抛异常 → 这些路径必须不触发）
"""
import asyncio
import json
from contextlib import ExitStack
from unittest import mock

import pytest

import niu_api.compat as compat


@pytest.fixture(autouse=True)
async def _clean_pipeline():
    """每个用例前复位全局队列/去重表/精灵状态（模块级全局，避免用例间串扰）。"""
    if compat._pipeline_queue is not None:
        await compat.stop_pipeline_queue()
    compat._SPIRIT_STATE = "idle"
    yield
    if compat._pipeline_queue is not None:
        await compat.stop_pipeline_queue()


NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete / 非 failure 的正常返回


class _FakeCalc:
    """计数缝：主 Agent 口径 count_message_single；build 口径 count_text（逐记录原文）。"""

    def count_message_single(self, role, content, tool_calls=None):
        return 100

    def count_text(self, text):
        return 100


#: F1 种子里的 DB 外尾段 id：严格 relay（strict_turn_ends）不剪末轮 → 它常驻 F1 尾段
TAIL_ID = "t5-tail-not-in-db"


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        # dream 阶段收尾补链（真函数依赖 LightRAG，测试桩空操作）
        return None


def _tidy_messages():
    msgs = []
    for i, mid in enumerate(["m1", "m2"]):
        m = mock.MagicMock()
        m.id = mid
        m.role = "user"
        m.content = f"hello {i}"
        m.tool_calls = None
        m.tool_call_id = None
        msgs.append(m)
    return msgs


def _cp_patches(sleep_side_effect, call_mock):
    """T5 独立 fixture（同 test_subagent_failure_cursor._tidy_failure_patches 模式，各 Task fixture 独立）。

    - is_sleeping → sleep_side_effect（检查点翻转驱动）
    - call_subagent_with_auto_answer → call_mock（记录被调用 agent 顺序）
    - 四个游标文件 READ 强制 cursor=''（Path.exists→False → 游标留空）
    - _write_cursor_with_lock → MagicMock（记录调用，测试 hermetic）
    """
    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("agent.context_assembler.calibration.get_ratio", return_value=1.0),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        # builder refetch lightrag 段——mock 隔离，不读真实用户配置
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        # 四个游标文件 READ 强制 cursor=''：Path.exists→False（缺失文件 → 游标留空）。
        # compat.py 在函数内 `from pathlib import Path`，无模块级 Path，故 patch 类方法本身
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("niu_api.compat._write_cursor_with_lock", create=True),
        mock.patch("niu_api.compat.is_sleeping", side_effect=sleep_side_effect),
    ]


def _run_sleep_tidy(sleep_side_effect, call_mock=None):
    """直接调 _tidy_context_impl sleep 分支（绕过 worker/CP0），驱动 CP1-CP3。

    v2 适配：向隔离 F1（conftest 已把 mdm.F1_PATH 指向 tmp）写入两条种子记录，
    使 entity 步真实执行（F1 空 → 生产代码跳过提炼）；F2 patch 到本次测试专用
    tmp 文件——relay 剪切目标只允许落测试文件。entity 默认返回带
    processed_line=999999（吸附到末个严格轮末 → 前缀剪切进 F2，末轮留 F1 尾段）。

    门控已随工程四重排摘除（决策 2），无 cursor_value 参。
    返回 (result, write_mock, call_mock, relay)，relay = {"f1":..., "f2":...} 供断言剪切语义。
    """
    import os as _os
    import tempfile

    import agent.md_mirror as mdm
    from niu_api.compat import _tidy_context_impl

    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
    if call_mock is None:
        call_mock = mock.MagicMock()

        call_mock.side_effect = _keyed_with_dream_report()
    relay = {}
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in _cp_patches(sleep_side_effect, call_mock):
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock", create=True))
        f2_path = _os.path.join(tempfile.mkdtemp(prefix="t5_cp_relay_"), "f2.md")
        stack.enter_context(mock.patch("agent.md_mirror.F2_PATH", f2_path))
        block = mdm.format_message_record(
            msg_id="m2", created_at="t", role="user", content="种子",
        )
        assert mdm.append_record(block, mdm.F1_PATH)
        # 严格 relay 不剪末轮：再种一条 DB 外 id 做 F1 尾段，前缀（m2）才剪得动
        block_tail = mdm.format_message_record(
            msg_id=TAIL_ID, created_at="t", role="user", content="种子尾段",
        )
        assert mdm.append_record(block_tail, mdm.F1_PATH)
        relay["f1"], relay["f2"] = mdm.F1_PATH, f2_path
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    return result, write_mock, call_mock, relay


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def _cursor_writes(write_mock):
    return [call.args[1] for call in write_mock.call_args_list]


def _turn_last_msg_ids_of(path):
    """文件里「每轮自末条向前第一条非空 msg_id」——轮边界取生产 mdm.all_turn_ends，id 取 mdm._msg_id_of。

    轮规则只在 md_mirror 里有一份，测试不复刻；F3 由 build_f3 从首个 user 记录起写，
    故 [0, 首个轮末) 窗口内不存在前置残片。
    """
    import agent.md_mirror as mdm

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


def _keyed_with_dream_report():
    """默认子 Agent mock：entity 报 processed_line，dream 报当前 F3 里真实存在的末轮末条 msg_id。"""
    import agent.md_mirror as mdm

    def _keyed(agent_name=None, **kwargs):
        if agent_name == "entity-extractor":
            return NORMAL_JSON + "\n处理完成 @end\nprocessed_line=999999"
        if agent_name == "dream-evolver":
            ids = _turn_last_msg_ids_of(mdm.F3_PATH)
            assert ids, f"F3 无完整轮末，种子不成立: {mdm.F3_PATH}"
            return NORMAL_JSON + f"\n处理完成 @end\nprocessed_msg_id={ids[-1]}"
        return NORMAL_JSON

    return _keyed


def test_cp1_interrupt_after_entity():
    """CP1：entity 段完成后唤醒 → interrupted；dream 不执行（T7：journal 腿迁 scheduler，entity 为首腿）。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return False  # 首次检查即 CP1（entity 后）

    result, write_mock, call_mock, relay = _run_sleep_tidy(sleep_side_effect)
    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock) == ["entity-extractor"], "entity 已执行，dream 不应执行"
    writes = _cursor_writes(write_mock)
    assert [d for d in writes if d.get("last_entity_extract_id")] == [], "entity UUID 游标已退役，零写"
    assert [d for d in writes if d.get("last_dream_evolve_id")] == []
    assert [d for d in writes if d.get("last_compress_id")] == [], "compress 游标已退役，零写"
    with open(relay["f1"], encoding="utf-8") as f:
        f1_text = f.read()
    assert f'"msg_id": "{TAIL_ID}"' in f1_text, "entity 已执行：F1 只剩未处理的末轮尾段"
    assert '"msg_id": "m2"' not in f1_text, "已提炼前缀应剪出 F1"
    with open(relay["f2"], encoding="utf-8") as f:
        assert '"msg_id": "m2"' in f.read(), "剪下前缀应已追加到 F2"
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# CP1-CP3：各检查点打断后后续步骤不执行、已推进游标不回滚
# ---------------------------------------------------------------------------
def test_dream_loop_interrupt_when_awake():
    """dream 循环内删除前缀后的中断检查：唤醒 → interrupted；F2 已删不回滚。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return calls["n"] < 2  # CP1（entity 后）仍睡眠，dream 循环内检查断

    result, write_mock, call_mock, relay = _run_sleep_tidy(sleep_side_effect)
    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver"]
    with open(relay["f2"], encoding="utf-8") as f:
        assert f.read() == "", "dream 首轮已删 F2 前缀，不回滚"
    assert calls["n"] == 2


def test_cp2_interrupt_after_dream_loop():
    """CP2：dream 循环完成后（纯中断检查）唤醒 → interrupted；无后续段可跳过。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return calls["n"] < 3  # CP1/dream 循环内检查仍睡眠，CP2 断

    result, write_mock, call_mock, _relay = _run_sleep_tidy(sleep_side_effect)
    assert result == {"status": "interrupted", "reason": "woke_up"}
    # T7 后管道序 entity→dream（journal 已迁 scheduler journal_daily）
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver"]
    writes = _cursor_writes(write_mock)
    assert [d for d in writes if d.get("last_entity_extract_id")] == [], "entity UUID 游标已退役，零写"
    assert [d for d in writes if d.get("last_dream_evolve_id")] == [], "dream 游标已退役（工程五），零写"
    assert [d for d in writes if d.get("last_compress_id")] == [], "compress 游标已退役，零写"
    assert calls["n"] == 3



def test_sleep_full_run_not_interrupted_when_asleep():
    """对照：全程睡眠 → 完整跑完（fixture 非空洞，CP 断言有判别力）。

    T7 后管道序 entity→dream→块摘要（journal 已迁 scheduler journal_daily）。
    """
    result, write_mock, call_mock, relay = _run_sleep_tidy(lambda: True)
    assert result.get("status") == "ok", f"睡眠中不应打断: {result}"
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver"]
    assert [d for d in _cursor_writes(write_mock) if d.get("last_compress_id")] == [], "compress 游标已退役，零写"
    with open(relay["f1"], encoding="utf-8") as f:
        f1_text = f.read()
    assert f'"msg_id": "{TAIL_ID}"' in f1_text, "完整跑完后 F1 只剩未处理的末轮尾段"
    assert '"msg_id": "m2"' not in f1_text, "已提炼前缀应剪出 F1"


# ---------------------------------------------------------------------------
# CP0：worker 层排队唤醒检查（T2 已实现，T5 补契约测试）
# ---------------------------------------------------------------------------

async def test_cp0_worker_cancel_when_not_sleeping(monkeypatch):
    """CP0：worker 取出 sleep 任务时非睡眠 → cancelled/woke_up，impl 零调用。"""
    called = []

    async def fake_impl(request, chat_lock_already_held=False):
        called.append(request)
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    monkeypatch.setattr(compat, "is_sleeping", lambda: False)
    compat.start_pipeline_queue()
    try:
        fut = compat._pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
        result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=1.0)
    finally:
        await compat.stop_pipeline_queue()
    assert result == {"status": "cancelled", "reason": "woke_up"}
    assert called == [], f"CP0 cancelled 时 impl 不应被调用: {called}"

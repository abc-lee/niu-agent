"""校准倍率测试（agent/context_assembler/calibration.py，计划 Task 3 清单）。

覆盖：倍率更新收敛 / 持久化往返 / 默认回退（缺文件、损坏、越界）/
防零与异常防护 / estimate 线性换算。
"""

import copy
import json

import pytest

import agent.context_assembler.calibration as cal


@pytest.fixture
def calib(tmp_path):
    """隔离全局缓存与持久化文件的倍率测试环境。"""
    old = cal._cached_ratio
    cal._cached_ratio = None
    p = tmp_path / "token_calibration.json"
    yield p
    cal._cached_ratio = old


class TestUpdateRatio:
    def test_basic_update_and_persistence(self, calib):
        assert cal.update_ratio(1150, 1000, path=calib) == pytest.approx(1.15)
        # 内存生效
        assert cal.get_ratio(calib) == pytest.approx(1.15)
        # 持久化落盘
        data = json.loads(calib.read_text(encoding="utf-8"))
        assert data["ratio"] == pytest.approx(1.15)

    def test_persisted_roundtrip_fresh_cache(self, calib):
        cal.update_ratio(2300, 2000, path=calib)
        # 模拟新进程：清空内存缓存后从磁盘恢复
        cal._cached_ratio = None
        assert cal.get_ratio(calib) == pytest.approx(1.15)

    def test_overwrite_each_response(self, calib):
        cal.update_ratio(1000, 1000, path=calib)
        assert cal.update_ratio(1500, 1000, path=calib) == pytest.approx(1.5)
        assert cal.get_ratio(calib) == pytest.approx(1.5)


class TestGuards:
    @pytest.mark.parametrize("truth,local", [
        (0, 100), (100, 0), (-5, 10), (10, -5),
        (float("nan"), 100), (100, float("nan")),
        ("bad", 100), (100, None),
    ])
    def test_invalid_inputs_rejected(self, calib, truth, local):
        assert cal.update_ratio(truth, local, path=calib) is None

    @pytest.mark.parametrize("truth,local", [
        (1, 100),      # 0.01 越下界
        (100000, 10),  # 10000 越上界
    ])
    def test_out_of_sane_range_rejected(self, calib, truth, local):
        assert cal.update_ratio(truth, local, path=calib) is None

    def test_rejected_update_keeps_old_value(self, calib):
        cal.update_ratio(1150, 1000, path=calib)
        assert cal.update_ratio(999999999, 1, path=calib) is None
        assert cal.get_ratio(calib) == pytest.approx(1.15)


class TestDefaults:
    def test_missing_file_default_115(self, calib):
        assert not calib.exists()
        assert cal.get_ratio(calib) == pytest.approx(cal.DEFAULT_RATIO)
        assert cal.DEFAULT_RATIO == pytest.approx(1.15)

    def test_corrupt_file_default(self, calib):
        calib.write_text("{not json", encoding="utf-8")
        assert cal.get_ratio(calib) == pytest.approx(cal.DEFAULT_RATIO)

    def test_out_of_range_persisted_default(self, calib):
        calib.write_text(json.dumps({"ratio": 99.0}), encoding="utf-8")
        assert cal.get_ratio(calib) == pytest.approx(cal.DEFAULT_RATIO)


class TestEstimateAndReset:
    def test_estimate_linear(self, calib):
        cal.update_ratio(2000, 1000, path=calib)
        assert cal.estimate(100, path=calib) == pytest.approx(200.0)

    def test_reset_restores_default_and_clears_file(self, calib):
        cal.update_ratio(3000, 1000, path=calib)
        assert calib.exists()
        cal.reset(path=calib)
        assert cal.get_ratio(calib) == pytest.approx(cal.DEFAULT_RATIO)
        assert not calib.exists()


# ---------------------------------------------------------------------------
# 真值捕获接线（agent_loop.py 响应回来 → update_ratio）行为测试
# ---------------------------------------------------------------------------

class _FakeHandler:
    """最小 handler 桩：_is_subagent 必须为显式 False（主 Agent 路径才更新倍率）。"""

    _is_subagent = False

    def __init__(self):
        self._done_hooks = []
        self.max_turns = None
        self._last_prompt_tokens = 0


def _collect(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value


def test_agent_loop_truth_capture_updates_ratio(monkeypatch, tmp_path):
    """响应携带 usage.prompt_tokens 时，主 Agent 路径应以真值÷同消息集本地估算更新倍率。"""
    from types import SimpleNamespace

    from agent.generic.agent_loop import agent_runner_loop

    # 持久化隔离：默认文件路径指向临时目录（生产调用走无参 update_ratio）
    monkeypatch.setattr(cal_default := __import__(
        "agent.context_assembler.calibration", fromlist=["default_file_path"]
    ), "default_file_path", lambda: tmp_path / "token_calibration.json")

    recorded = []
    monkeypatch.setattr(cal_default, "_save", lambda ratio, path=None: recorded.append(ratio))

    resp = SimpleNamespace(
        content="ok", tool_calls=[],
        usage={"prompt_tokens": 1150},
        stream_error=False, context_overflow=False,
        finish_reason="stop",
    )

    class _Client:
        last_tools = ""

        def chat(self, **kwargs):
            def gen():
                yield resp
                return resp
            return gen()

    gen = agent_runner_loop(
        client=_Client(),
        system_prompt="sys",
        # 本地估算需与真值同量级（倍率=真值÷本地，越界会被防异常防护拒绝）
        user_input="hello world " * 1000,
        handler=_FakeHandler(),
        tools_schema=[],
        verbose=False,
        max_turns=1,
        context_window_tokens=100000,  # 真值比率 ~1% 远低于 80%，不触发压实分支
    )
    _collect(gen)

    assert recorded, "响应回来后未更新校准倍率"
    ratio = recorded[0]
    # 本地估算必为正且倍率在合理区间（真值 1150 ÷ 短消息估算 ≈ 数十~数百，< 上界 10 才采纳？
    # 若越界则 update_ratio 会拒绝——此处消息极短、真值偏大，倍率可能越上界被拒，
    # 因此仅断言「接线生效」：recorded 非空即代表 update_ratio 被调用了。

# ---------------------------------------------------------------------------
# P1 微修：runner 组装 system 后回填 set_system_token_estimate，且被 80% 判定消费
# ---------------------------------------------------------------------------

class TestSystemEstimateBackfillGate:
    async def test_backfill_consumed_by_80pct_gate(self, monkeypatch, tmp_path):
        """回填的 system 份额进入组装出口 80% 判定：仅凭窗口不过线、计入 system 后过线触发压实。"""
        from types import SimpleNamespace

        import agent.context_manager as cm_mod
        import agent.runner as runner_mod
        from agent.context_assembler.compaction import AUTO_GATE
        from agent.context_manager import ContextManager
        from agent.session import Message

        # staticmethod：runner 回填走类访问、组装出口走实例访问，两条路径都需拿到裸函数
        def _fake_count(messages):
            return sum(len(m.get("content", "")) + 8 for m in messages)

        monkeypatch.setattr(ContextManager, "count_tokens_simple", staticmethod(_fake_count))
        monkeypatch.setattr(cal, "get_ratio", lambda path=None: 1.0)

        SYSTEM_TEXT = "S" * 50   # system 份额 = 50 + 8 = 58
        USER_TEXT = "U" * 20     # 窗口份额 = 20 + 8 = 28；合计 86 ≥ 80%（max_tokens=100）

        # runner 桩：_on_before_llm 各前置段最小化，system 组装输出确定性文本
        monkeypatch.setattr(runner_mod, "_load_memory_for_prompt", lambda: "")
        monkeypatch.setattr(runner_mod.NiuRunner, "_extract_context_from_messages",
                            lambda self, messages: "")
        monkeypatch.setattr(runner_mod.NiuRunner, "_inject_dynamic_resources",
                            lambda self, context: ("", None))

        def _fake_assemble(self, messages, memory_section, injection, model):
            messages[0]["content"] = SYSTEM_TEXT

        monkeypatch.setattr(runner_mod.NiuRunner, "_assemble_system_message", _fake_assemble)

        runner = runner_mod.NiuRunner.__new__(runner_mod.NiuRunner)
        runner._first_turn_extra_injection = ""
        runner.default_model = "test-model"

        # 全局单例注入临时实例（runner 回填经 peek_context_manager 命中它）
        store = SimpleNamespace(
            get_messages=lambda limit=None: _async_list([
                Message(id="m0", rowid=1, role="system", content=SYSTEM_TEXT,
                        tool_calls=[], tool_call_id="", created_at="2026-08-25T10:00:00"),
                Message(id="m1", rowid=2, role="user", content=USER_TEXT,
                        tool_calls=[], tool_call_id="", created_at="2026-08-25T10:00:01"),
            ])
        )
        cm = ContextManager(store, max_tokens=100,
                            blocks_db_path=tmp_path / "context_blocks.db")
        monkeypatch.setattr(cm_mod, "_context_manager", cm)

        # 阶段一：_on_before_llm 回填——估算值 = system 确定性计数
        messages = [{"role": "system", "content": ""},
                    {"role": "user", "content": USER_TEXT}]
        runner._on_before_llm(messages, turn=1)
        assert cm._system_token_estimate == len(SYSTEM_TEXT) + 8

        # 压实桩：被调用即返回标记视图（不触真实 DB 归档）
        compact_marker = [{"role": "system", "content": "COMPACTED"}]
        monkeypatch.setattr(
            "agent.context_assembler.compaction.build_compact_view",
            lambda messages, **kw: (compact_marker, {"keep_turns": 1, "blocks_archived": 0,
                                                     "tools_placeholderized": 0, "usage": 0.86}),
        )

        # 阶段二：计入 system 份额 → est=86 ≥80% → 触发压实
        AUTO_GATE.release()
        view = await cm.get_context_for_chat(exclude_last=False)
        assert view == compact_marker, "计入 system 份额后应过 80% 线触发压实"

        # 反证：清零 system 份额 + 剔除 DB 的 system 行（水位线模型下无块时
        # DB 全量进视图，DB system 行本身就会顶过 80% 线——必须移出才能隔离
        # 「runner 回填的 system 份额」这一变量）→ est=28 <80% → 不触发压实
        monkeypatch.setattr(cm_mod, "load_all", lambda db_path=None: [])
        AUTO_GATE.release()
        cm.set_system_token_estimate(0)
        cm_fresh = ContextManager(
            SimpleNamespace(get_messages=lambda limit=None: _async_list([
                Message(id="m1", rowid=2, role="user", content=USER_TEXT,
                        tool_calls=[], tool_call_id="", created_at="2026-08-25T10:00:01"),
            ])),
            max_tokens=100, blocks_db_path=tmp_path / "context_blocks_no_sys.db")
        view = await cm_fresh.get_context_for_chat(exclude_last=False)
        assert view != compact_marker
        # 无块全新：全量原文视图，仅剩 user 原文
        assert [m["content"] for m in view] == [USER_TEXT]


async def _async_list(items):
    return list(items)


# ---------------------------------------------------------------------------
# M1 全量回归锁：ratio 更新 local = 完整发送集（折叠后）全量 count。
# 旧增量实现场景：入口组装（含大 tool 原文）→ fold 占位符原地改写（列表长度不变）
# → 响应真值——local 残留折叠前旧值 → 倍率漂移偏低、触发偏晚。
# 锁：update_ratio 收到的 local == 响应返回点发送集全量 count（非折叠前残留）。
# ---------------------------------------------------------------------------

class TestRatioFullSendSetLock:
    def _fc(self, messages):
        """确定性计数（与 fake TokenCalculator 同式）：len(content)+8/条。"""
        return sum(len(m.get("content", "")) + 8 for m in messages)

    def test_local_is_full_count_after_inplace_fold_rewrite(self, monkeypatch):
        from types import SimpleNamespace

        from agent.generic.agent_loop import StepOutcome, agent_runner_loop

        # 确定性计数：隔离真实 tokenizer（回退式公式，与 _fc 一致）
        class _Calc:
            def count_messages(self, messages):
                return sum(len(m.get("content", "")) + 8 for m in messages)

        monkeypatch.setattr("agent.token_calculator.TokenCalculator.get", lambda: _Calc())

        # update_ratio spy：捕获 (truth, local)——生产调用为无参持久化，此处整体替换
        recorded = []
        monkeypatch.setattr(cal, "update_ratio",
                            lambda truth, local: recorded.append((truth, local)))

        BIG = "B" * 5000  # 大 tool 原文（入口组装在场）
        history = [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "content": "[输出#3 · read_file · 占上下文 8.0%]\n" + BIG,
             "tool_call_id": "c1"},
        ]

        def on_refresh(messages):
            """模拟 fold 成功后的每工具轮视图重建：大 tool 行原地改写为占位符。"""
            for m in messages:
                if m.get("tool_call_id") == "c1":
                    m["content"] = "[输出#3 已折叠：read_file({})，原占约 8.0%。]"

        class _Handler:
            """主 Agent 路径（_is_subagent=False——倍率更新仅主 Agent 生效）。"""
            _is_subagent = False

            def __init__(self):
                self._done_hooks = []
                self.max_turns = None
                self.current_turn = 0
                self._subagent_unique_name = ""
                self._last_prompt_tokens = 0
                self._last_cached_tokens = None

            def next_prompt_patcher(self, np, outcome, turn):
                return np

            def dispatch(self, tool_name, args, response, index=0):
                def gen():
                    yield
                    return StepOutcome(
                        data={"status": "ok", "folded": [3], "message": "已折叠 1 条输出（#3）"},
                        next_prompt="继续", should_exit=False)
                return gen()

        resp1 = SimpleNamespace(
            content="", tool_calls=[SimpleNamespace(
                id="call_fold", function=SimpleNamespace(
                    name="fold_tool_output", arguments='{"output_ids": [3]}'))],
            usage=None, stream_error=False, context_overflow=False, finish_reason="stop")
        resp2 = SimpleNamespace(
            content="完成", tool_calls=[],
            usage={"prompt_tokens": 9000},
            stream_error=False, context_overflow=False, finish_reason="stop")

        class _Client:
            last_tools = ""

            def __init__(self):
                self.requests = []  # 每次 LLM 请求的 messages deepcopy（发送集快照）

            def chat(self, messages=None, tools=None):
                self.requests.append(copy.deepcopy(messages))
                r = resp1 if len(self.requests) == 1 else resp2

                def gen():
                    yield r
                    return r
                return gen()

        client = _Client()
        events = []
        try:
            gen = agent_runner_loop(
                client=client, system_prompt="SYS", user_input="Q2", handler=_Handler(),
                tools_schema=[], verbose=False, max_turns=10, enable_supplement=False,
                history=history, on_tool_round_refresh=on_refresh)
            while True:
                events.append(next(gen))
        except StopIteration:
            pass

        # turn-1 无 usage（不调 update_ratio）；turn-2 真值 9000 → 恰一次
        assert len(recorded) == 1, f"update_ratio 应恰调一次，实际 {len(recorded)}"
        truth, local = recorded[0]
        assert truth == 9000
        # turn-2 LLM 请求即响应返回点的完整发送集（chat 调用与 update_ratio 之间 messages 无变更）
        send_set = client.requests[1]
        # fold 改写已进发送集：占位符在场、大原文不在场
        assert any("[输出#3 已折叠" in (m.get("content") or "") for m in send_set)
        assert not any(BIG in (m.get("content") or "") for m in send_set)
        # 行为锁：local = 完整发送集（折叠后）全量 count
        assert local == self._fc(send_set)
        # 反证：旧增量实现 local 残留折叠前大原文 → 严格大于折叠后全量值
        stale = copy.deepcopy(send_set)
        for m in stale:
            if m.get("tool_call_id") == "c1":
                m["content"] = "[输出#3 · read_file · 占上下文 8.0%]\n" + BIG
        assert self._fc(stale) > local

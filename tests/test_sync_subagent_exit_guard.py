"""同步子 Agent 挂起丢失防护测试（T3）：退出前警告 + cleanup 场景保留。

计划：docs/superpowers/plans/2026-08-31-sync-subagent-exit-guard.md T3 节（冻结 v6）。
全 mock 禁真 LLM；每个测试前后清空 SubagentRegistry._instances
（T1 新代码读全局 registry——跨测试泄漏会向无关 agent_loop 测试注入警告）。

10 组用例：
  T3-1 拦截式警告：主 Agent 无工具调用退出 + 同步挂起实例 → 第一轮 Path A 注入 user 警告并 continue；
       第二轮纯文本 → 不再注入 → 正常 CURRENT_TASK_DONE；挂起实例仍在 registry
  T3-2 警告后 LLM 调 chat-with-xxx(answer=, unique_name=) → 走既有 answer 分支接续挂起（mock _run_agent_loop）
  T3-3 无挂起实例 → 不注入警告（首轮直接退出）
  T3-4 仅异步 running 实例（is_sync=False）→ 不注入警告
  T3-4b 程序触发同步挂起实例（source="program"，睡眠管道等）→ 不注入警告（与主 Agent 无关）
  T3-5 子 Agent 路径（handler._is_subagent=True）→ 不注入警告
  T3-6 警告不进 db：yield 的 StreamEvent 无警告 persist；rv["messages"] 尾部 user 警告被 persist_agent_reply 跳过
  T3-7 cleanup 判定矩阵 + runner finally 接线（cleanup_suspended_sync_subagents(return_value) 传参）
  T3-8 cleanup 不推送清理通知
  T3-9 /clear（clear_chat）reset_derived_state() 旁以 STOPPED 语义调 cleanup；空闲态残留实例被清理
  T3-10 其余 3 个会话清空端点（chat.py clear_session / session.py delete_messages / delete_session）同族调 cleanup
"""
import json
from unittest import mock

import pytest

from agent.generic.agent_loop import StepOutcome, StreamEvent
from agent.subagent_registry import SubagentRegistry


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前后清空全局 registry（T1 新代码读全局 registry，防跨测试泄漏）。"""
    SubagentRegistry._instances.clear()
    yield
    SubagentRegistry._instances.clear()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _register_suspended(name="nutritionist", is_sync=True, state="waiting_for_answer"):
    """真实 register 一个实例并置指定状态（register 默认 state=running）。"""
    SubagentRegistry.register(name, mock.MagicMock(), force_unique_name=name)
    inst = SubagentRegistry.get(name)
    inst.is_sync = is_sync
    inst.state = state
    return inst


def _resp(content="", tool_calls=None):
    """非 verbose 路径响应对象。显式置假值，避免 MagicMock 自动真值误入 LLM_ERROR 分支。"""
    r = mock.MagicMock()
    r.stream_error = False
    r.error_msg = None
    r.content = content
    r.tool_calls = tool_calls if tool_calls is not None else []
    r.finish_reason = "stop"
    r.usage = None
    r.context_overflow = False
    r.thinking = None
    return r


def _tool_call(name, args, tc_id="call_1"):
    tc = mock.MagicMock()
    tc.id = tc_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args, ensure_ascii=False)
    return tc


def _make_client(responses):
    """client.chat 每次调用返回 generator：yield 一个空 chunk + return resp（exhaust 取 return 值）。"""
    client = mock.MagicMock()
    it = iter(responses)

    def _chat_gen(messages, tools):
        resp = next(it)

        def gen():
            yield  # exhaust 消费；StopIteration.value = resp 被 exhaust 返回
            return resp

        return gen()

    client.chat.side_effect = _chat_gen
    return client


class _LoopHandler:
    """驱动 agent_runner_loop 的最小 handler（真实类，避免 MagicMock 真值陷阱）。"""

    def __init__(self, is_subagent=False):
        self._is_subagent = is_subagent
        self._done_hooks = []
        self.max_turns = None
        self.current_turn = 0
        self._current_messages = None
        self._last_prompt_tokens = 0
        self._last_cached_tokens = None

    def next_prompt_patcher(self, prompt, resp, turn):
        return prompt


def _run_loop(handler, responses, max_turns=2):
    """驱动 agent_runner_loop 到耗尽。返回 (client, events, result)。

    agent_runner_loop 内部 `from agent.runner import clear_stop, is_stop_requested`
    （调用时解析），因此 patch agent.runner.* 而非 agent.generic.agent_loop.*。
    """
    from agent.generic.agent_loop import agent_runner_loop

    client = _make_client(responses)
    events = []
    with mock.patch("agent.runner.clear_stop"), \
         mock.patch("agent.runner.is_stop_requested", return_value=False), \
         mock.patch("agent.generic.agent_loop.count_messages_tokens", return_value=0), \
         mock.patch("agent.generic.agent_loop._read_warning_threshold", return_value=0.8):
        gen = agent_runner_loop(
            client=client, system_prompt="",
            system_message={"role": "system", "content": ""},
            user_input="hi", handler=handler, tools_schema=[],
            max_turns=max_turns, verbose=False, enable_supplement=False)
        try:
            while True:
                events.append(next(gen))
        except StopIteration as e:
            result = e.value
    return client, events, result


def _warnings_in(messages):
    return [m for m in messages if m.get("role") == "user" and "[系统警告]" in (m.get("content") or "")]


# ---------------------------------------------------------------------------
# T3-1 拦截式警告
# ---------------------------------------------------------------------------

def test_intercept_warning_injects_once_then_exits():
    """T3-1：主 Agent 无工具调用退出 + 同步挂起实例 → 第一轮 Path A 注入 user 警告并 continue；
    第二轮纯文本 → 不再注入 → 正常 CURRENT_TASK_DONE；挂起实例仍在 registry（场景不丢）。"""
    _register_suspended("nutritionist")
    handler = _LoopHandler(is_subagent=False)
    client, events, result = _run_loop(handler, [_resp("任务完成"), _resp("收到")], max_turns=2)

    assert result["result"] == "CURRENT_TASK_DONE"
    assert client.chat.call_count == 2  # 第一轮被 continue 拦截（未 return）→ LLM 共调两次
    warns = _warnings_in(result["messages"])
    assert len(warns) == 1  # 最多注入一次（_sync_suspend_warned 守卫）
    w = warns[0]["content"]
    assert "nutritionist" in w                    # 含挂起实例名
    assert "确定要退出" in w and "nutritionist" in w  # 警告只提醒+询问（不教方法，教学在 niu.md）
    assert SubagentRegistry.get("nutritionist") is not None  # 挂起实例保留


# ---------------------------------------------------------------------------
# T3-2 警告后 LLM 走 answer 分支接续
# ---------------------------------------------------------------------------

def test_warning_then_answer_branch_resumes():
    """T3-2：第一轮注入警告 continue → 第二轮 LLM 调 chat-with-xxx(answer=, unique_name=)
    → 走既有 answer 分支接续挂起（mock _run_agent_loop）；接续完成后注销。"""
    inst = _register_suspended("nutritionist")
    inst.suspended_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    inst.suspended_handler = mock.MagicMock()
    inst.suspended_client = mock.MagicMock()
    inst.suspended_tools_schema = []
    inst.suspended_system_message = {"role": "system", "content": "sys"}

    captured_dispatch = []

    class _DispatchingHandler(_LoopHandler):
        def dispatch(self, tool_name, args, response, index=0):
            captured_dispatch.append((tool_name, dict(args)))

            def gen():
                from agent.subagent import call_subagent
                agent_name = tool_name[len("chat-with-"):]
                result = call_subagent(
                    agent_name=agent_name,
                    task=args.get("task", ""),
                    llm_config={"model": "test-model"},
                    answer=args.get("answer"),
                    answer_unique_name=(args.get("unique_name") or agent_name) if args.get("answer") else None,
                )
                yield  # exhaust 消费；StopIteration.value = StepOutcome
                return StepOutcome(data=result, next_prompt="ok")

            return gen()

    responses = [
        _resp("任务完成"),  # 第一轮：纯文本 → 注入警告 + continue
        _resp("", tool_calls=[_tool_call(
            "chat-with-nutritionist", {"answer": "继续", "unique_name": "nutritionist"})]),  # 第二轮：answer 分支
        _resp("完成"),  # 第三轮：纯文本 → 正常退出（不再注入警告）
    ]

    with mock.patch("agent.subagent.get_subagent_config", return_value={}), \
         mock.patch("agent.subagent.build_subagent_system_segments", return_value=("static", "")), \
         mock.patch("agent.runner.create_client"), \
         mock.patch("agent.subagent.get_subagent_mcp_tools_schema", return_value=None), \
         mock.patch("agent.subagent._build_subagent_tools_schema", return_value=[]), \
         mock.patch("agent.subagent._read_context_window_tokens", return_value=0), \
         mock.patch("agent.runner.get_runner", return_value=None), \
         mock.patch("agent.subagent._maybe_push_subagent_instruction", return_value=False), \
         mock.patch("agent.subagent._run_agent_loop") as ral:
        ral.return_value = ("ok", {"result": "CURRENT_TASK_DONE"}, "ok")
        client, events, result = _run_loop(_DispatchingHandler(), responses, max_turns=3)

    assert result["result"] == "CURRENT_TASK_DONE"
    # 第一轮警告注入（continue 证据：LLM 共调 3 次）
    assert client.chat.call_count == 3
    assert len(_warnings_in(result["messages"])) == 1
    # 第二轮走 answer 分支：dispatch 收到 chat-with-nutritionist(answer=, unique_name=)
    assert captured_dispatch == [("chat-with-nutritionist", {"answer": "继续", "unique_name": "nutritionist"})]
    # 挂起接续：_run_agent_loop 以 resumed_messages 调用（尾部 = 主 Agent 回答）
    assert ral.call_count == 1
    resumed = ral.call_args.kwargs["resumed_messages"]
    assert resumed is inst.suspended_messages
    assert resumed[-1] == {"role": "user", "content": "[主 Agent 回答] 继续"}
    # 接续完成（非挂起态）→ 注销
    assert SubagentRegistry.get("nutritionist") is None


# ---------------------------------------------------------------------------
# T3-3 / T3-4 / T3-5 不注入警告的三种场景
# ---------------------------------------------------------------------------

def test_no_suspended_no_warning():
    """T3-3：无挂起实例 → 不注入警告（首轮直接退出）。"""
    handler = _LoopHandler(is_subagent=False)
    client, events, result = _run_loop(handler, [_resp("任务完成")], max_turns=2)

    assert result["result"] == "CURRENT_TASK_DONE"
    assert client.chat.call_count == 1  # 首轮直接退出，无 continue
    assert _warnings_in(result["messages"]) == []


def test_async_running_instance_no_warning():
    """T3-4：仅异步 running 实例（is_sync=False）→ 不注入警告。"""
    _register_suspended("async-worker", is_sync=False, state="running")
    handler = _LoopHandler(is_subagent=False)
    client, events, result = _run_loop(handler, [_resp("任务完成")], max_turns=2)

    assert result["result"] == "CURRENT_TASK_DONE"
    assert client.chat.call_count == 1
    assert _warnings_in(result["messages"]) == []


def test_program_source_sync_suspended_no_warning():
    """T3-4b：程序触发同步挂起实例（source="program"，睡眠管道 entity-extractor/dream-evolver 等）
    → 不注入警告（与主 Agent 无关，首轮直接退出）；对照 T3-1（source 默认 user → 注入）。"""
    _register_suspended("entity-extractor").source = "program"
    handler = _LoopHandler(is_subagent=False)
    client, events, result = _run_loop(handler, [_resp("任务完成")], max_turns=2)

    assert result["result"] == "CURRENT_TASK_DONE"
    assert client.chat.call_count == 1  # 首轮直接退出，无 continue
    assert _warnings_in(result["messages"]) == []


def test_subagent_path_no_warning():
    """T3-5：子 Agent 路径（handler._is_subagent=True）→ 不注入警告（子 Agent 有自己的 @ 拦截）。"""
    _register_suspended("nutritionist")
    handler = _LoopHandler(is_subagent=True)
    client, events, result = _run_loop(handler, [_resp("任务完成")], max_turns=2)

    assert result["result"] == "CURRENT_TASK_DONE"
    assert client.chat.call_count == 1
    assert _warnings_in(result["messages"]) == []


# ---------------------------------------------------------------------------
# T3-6 警告不进 db
# ---------------------------------------------------------------------------

async def test_warning_not_persisted_to_db():
    """T3-6：警告不进 db——① yield 的 StreamEvent 无警告 persist 事件；
    ② rv["messages"] 尾部 user 警告被 persist_agent_reply 的 role=user skip 跳过未落库。"""
    _register_suspended("nutritionist")
    handler = _LoopHandler(is_subagent=False)
    client, events, result = _run_loop(handler, [_resp("任务完成"), _resp("收到")], max_turns=2)

    # ① yield 的 persist 事件中无警告文本（警告只注入 messages，不 yield persist）
    persist_payloads = [e.content for e in events if isinstance(e, StreamEvent) and e.type == "persist"]
    assert all("[系统警告]" not in p for p in persist_payloads)

    # ② rv["messages"] 尾部 user 警告 → persist_agent_reply 跳过 role=user
    warns = _warnings_in(result["messages"])
    assert len(warns) == 1
    from niu_api.chat import persist_agent_reply

    store_calls = []

    class FakeStore:
        async def add_message(self, role, content, **kw):
            store_calls.append({"role": role, "content": content})
            return f"id-{len(store_calls)}"

    rv = {"result": "CURRENT_TASK_DONE", "messages": [
        {"role": "system", "content": ""},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "任务完成"},
        warns[0],  # 尾部 user 警告
    ]}
    with mock.patch("niu_api.chat.notify_new_message", new=mock.AsyncMock(return_value=True)):
        await persist_agent_reply(store=FakeStore(), rv=rv, history_len=1, full_reply="")

    assert len(store_calls) == 1
    assert store_calls[0]["role"] == "assistant"
    assert all(c["role"] != "user" for c in store_calls)  # role=user 被跳过未落库


# ---------------------------------------------------------------------------
# T3-7 cleanup 判定矩阵 + runner finally 接线
# ---------------------------------------------------------------------------

def test_cleanup_decision_matrix():
    """T3-7a：cleanup 判定矩阵——仅用户显式停止清理；正常退出/异常退出保留场景。"""
    from agent.runner import cleanup_suspended_sync_subagents

    cases = [
        ({"result": "CURRENT_TASK_DONE"}, False, True),      # 正常退出 → 保留
        ({"result": "STOPPED"}, False, False),               # 用户 /stop → 清理
        ({"result": "TERMINATED_BY_SUPPLEMENT"}, False, False),  # supplement 队列终止 → 清理
        (None, False, True),                                 # 异常退出（return_value=None）→ 保留
    ]
    for i, (rv, stop_flag, should_keep) in enumerate(cases):
        name = f"agent-{i}"
        _register_suspended(name)
        with mock.patch("agent.runner.is_stop_requested", return_value=stop_flag):
            cleanup_suspended_sync_subagents(rv)
        assert (SubagentRegistry.get(name) is not None) is should_keep

    # gen.close() 路径：return_value=None + 全局停止标志置位 → 清理
    _register_suspended("nutritionist")
    with mock.patch("agent.runner.is_stop_requested", return_value=True):
        cleanup_suspended_sync_subagents(None)
    assert SubagentRegistry.get("nutritionist") is None


def test_runner_finally_passes_return_value():
    """T3-7b：runner 工具循环 finally 调 cleanup_suspended_sync_subagents(return_value) 传参正确
    （钉住调用点 1 接线，防回退旧无参调用）。"""
    from agent import runner as runner_mod
    from agent.runner import NiuRunner

    captured = []

    def fake_loop(**kwargs):
        def gen():
            if False:
                yield  # 成为 generator 但不 yield 任何值；首个 next() 直接 StopIteration(value=dict)
            return {"result": "STOPPED", "messages": []}
        return gen()

    self_mock = mock.MagicMock()
    self_mock._im_channel_id = ""
    self_mock.default_model = "test-model"
    self_mock.base_tools_schema = []
    self_mock._assemble_tools_schema.return_value = []
    self_mock.should_push_im.return_value = False

    with mock.patch.object(runner_mod, "cleanup_suspended_sync_subagents",
                           side_effect=lambda rv=None: captured.append(rv)), \
         mock.patch.object(runner_mod, "agent_runner_loop", side_effect=fake_loop), \
         mock.patch.object(runner_mod, "is_stop_requested", return_value=False), \
         mock.patch.object(runner_mod, "clear_stop"), \
         mock.patch("agent.subagent._read_context_window_tokens", return_value=0):
        list(NiuRunner.chat(self_mock, session_id="s1", user_input="hi"))

    assert captured == [{"result": "STOPPED", "messages": []}]


# ---------------------------------------------------------------------------
# T3-8 cleanup 不推送清理通知
# ---------------------------------------------------------------------------

def test_cleanup_no_push_notification():
    """T3-8：cleanup 不推送清理通知（2026-08-11 用户拍板）——工具错误/orphan 反馈已告知主 Agent，
    通知以 user 消息混入对话流会被误认为用户话。"""
    from agent.runner import cleanup_suspended_sync_subagents

    _register_suspended("nutritionist")
    pushed = []
    import agent.main_agent_request_queue as q_mod
    with mock.patch.object(q_mod, "get_main_agent_request_queue",
                           return_value=type("_Q", (), {"push": staticmethod(lambda c: pushed.append(c))})()):
        cleanup_suspended_sync_subagents({"result": "STOPPED"})

    assert SubagentRegistry.get("nutritionist") is None  # 已清理
    assert pushed == []  # 不推送


# ---------------------------------------------------------------------------
# T3-9 /clear（clear_chat）清理
# ---------------------------------------------------------------------------

def _patch_clear_chat_deps(monkeypatch, capture_cleanup=True):
    """clear_chat 依赖 mock：store/runner/tmp/md_mirror/reset_derived_state。
    capture_cleanup=True 时捕获 cleanup 调用参数；False 时保留真实 cleanup（行为断言用）。"""
    from niu_api import compat

    captured = []
    if capture_cleanup:
        monkeypatch.setattr("agent.runner.cleanup_suspended_sync_subagents",
                            lambda rv=None: captured.append(rv))
    else:
        # 隔离全局停止标志（真实 cleanup 读 agent.runner.is_stop_requested）
        monkeypatch.setattr("agent.runner.is_stop_requested", lambda: False)
    monkeypatch.setattr("agent.runner.request_stop", lambda: None)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplements", lambda: None)

    class FakeStore:
        async def clear_messages(self):
            return 2

    class FakeRunner:
        handler = None
        _decay_pool = []
        _brain_injector = None

    async def fake_get_message_store():
        return FakeStore()

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: FakeRunner())
    monkeypatch.setattr("agent.tmp_dir.cleanup_all_tmp", lambda: 0)
    monkeypatch.setattr("agent.md_mirror.truncate_relay_files", lambda: None)
    monkeypatch.setattr("agent.context_assembler.reset_derived_state", lambda *a, **k: None)
    return captured


async def test_clear_chat_calls_cleanup_with_stopped(monkeypatch):
    """T3-9a：/clear（clear_chat）在 reset_derived_state() 旁以 STOPPED 语义调用 cleanup。"""
    from niu_api import compat

    captured = _patch_clear_chat_deps(monkeypatch, capture_cleanup=True)
    result = await compat.clear_chat(mock.MagicMock())

    assert result["success"] is True
    assert captured == [{"result": "STOPPED"}]  # STOPPED 语义（显式放弃当前全部工作）


async def test_clear_chat_behavioral_unregisters_suspended(monkeypatch):
    """T3-9b（行为断言）：空闲态残留的挂起同步实例在 /clear 后被真实注销。"""
    from niu_api import compat

    _patch_clear_chat_deps(monkeypatch, capture_cleanup=False)
    _register_suspended("nutritionist")
    result = await compat.clear_chat(mock.MagicMock())

    assert result["success"] is True
    assert SubagentRegistry.get("nutritionist") is None  # 残留实例被清理


# ---------------------------------------------------------------------------
# T3-10 其余 3 个会话清空端点同族
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["clear_session", "delete_messages", "delete_session"])
async def test_other_clear_endpoints_call_cleanup(monkeypatch, endpoint):
    """T3-10：chat.py clear_session / session.py delete_messages / delete_session 均在
    clear_messages + reset_derived_state 旁以 STOPPED 语义调用 cleanup。"""
    from niu_api import chat as chat_module
    from niu_api import session as session_mod

    captured = []
    cleared = []
    monkeypatch.setattr("agent.runner.cleanup_suspended_sync_subagents",
                        lambda rv=None: captured.append(rv))
    monkeypatch.setattr("agent.context_assembler.reset_derived_state", lambda *a, **k: None)

    class FakeStore:
        async def clear_messages(self):
            cleared.append(True)
            return 3

    async def fake_get_message_store():
        return FakeStore()

    monkeypatch.setattr(chat_module, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(session_mod, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: None)

    if endpoint == "clear_session":
        result = await chat_module.clear_session("s1")
        assert result["status"] == "ok"
    elif endpoint == "delete_messages":
        result = await session_mod.delete_messages("s1")
        assert result["deleted_count"] == 3
    else:
        result = await session_mod.delete_session("s1")
        assert result["deleted"] is True

    assert cleared  # clear_messages 已执行
    assert captured == [{"result": "STOPPED"}]  # reset_derived_state 旁以 STOPPED 语义调 cleanup

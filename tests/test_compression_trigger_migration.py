"""Task 6：触发口收敛——响应后压实删除 + /compact 消息拦截（全 mock，禁真实 LLM）。

行为锁（spec 2026-09-06）：
1. _is_compression_command 识别（精确匹配、strip、大小写不敏感）
2. chat_session 忙时 /compact → 置 manual 意图 + 不落库 + 不入 supplement + 立即返回
3. chat_session 闲时 /compact → 直调受控压缩（mock run_controlled_compression）+ finally consume
4. agent_loop 响应后不再压实（主 Agent）：usage 达线 → 不置压缩意图、不闩 AUTO_GATE（on_context_high_usage 参数已删）
5. 组装出口置 auto 意图（Step 4b）：get_context_for_chat 达线 → request_compression("auto") + 不就地压实
6. manual 意图 + 提炼失败冷却期：门 defer（不落空转、client.chat 仍发生、manual 意图重设）
"""
from unittest.mock import MagicMock, Mock, patch

import pytest

# 导入序守卫：agent.context_assembler.compaction 顶层依赖 context_manager.ContextManager
# （既有循环依赖）——必须先导入 assembler 包（该方向完整加载），否则先 import
# agent.context_manager 会命中部分初始化模块 ImportError。
import agent.context_assembler  # noqa: F401,E402


# ---------------------------------------------------------------------------
# 1. _is_compression_command helper
# ---------------------------------------------------------------------------

def test_is_compression_command_exact():
    from niu_api.compat import _is_compression_command
    assert _is_compression_command("/compact") is True
    assert _is_compression_command(" /compact ") is True  # strip
    assert _is_compression_command("/COMPACT") is True  # 大小写不敏感
    assert _is_compression_command("/compactx") is False
    assert _is_compression_command("compact") is False  # 无斜杠
    assert _is_compression_command("") is False
    assert _is_compression_command(None) is False


# ---------------------------------------------------------------------------
# 2. /compact 忙时：置意图 + 不落库 + 不入 supplement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_busy_sets_intent_no_add_message(monkeypatch):
    """chat_session 锁忙（_chat_lock.locked()=True）+ message=/compact →
    request_compression('manual') + 不调 add_message + 不入 supplement + 立即返回。"""
    import niu_api.compat as compat

    # 置忙：占用 _chat_lock（本测试协程内 acquire 后不 release，模拟 agent 持锁）
    await compat._chat_lock.acquire()
    try:
        requested = []
        monkeypatch.setattr(compat, "request_compression", lambda r: requested.append(r))

        add_calls = []

        class _Store:
            async def add_message(self, **kw):
                add_calls.append(kw)
                return "id-1"

        async def _fake_store():
            return _Store()

        monkeypatch.setattr(compat, "get_message_store", _fake_store)
        supp = []
        monkeypatch.setattr("agent.runner.enqueue_supplement", lambda m: supp.append(m))

        from niu_api.compat import ChatRequest
        resp = await compat.chat_session(ChatRequest(message="/compact"))
        assert requested == ["manual"]
        assert add_calls == [], "忙时 /compact 不得落库"
        assert supp == [], "忙时 /compact 不得入 supplement 队列"
        assert resp.reply  # 立即返回（排队提示）
    finally:
        compat._chat_lock.release()


# ---------------------------------------------------------------------------
# 3. /compact 闲时：直调受控压缩 + finally consume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_idle_direct_compression(monkeypatch):
    """锁空闲 + message=/compact → 不走正常消息路径（不 add_message、不进 agent_runner_loop），
    直调 run_controlled_compression（mock）+ finally consume 意图。"""
    import niu_api.compat as compat

    assert not compat._chat_lock.locked()
    requested = []
    consumed = []
    monkeypatch.setattr(compat, "request_compression", lambda r: requested.append(r))
    monkeypatch.setattr(compat, "consume_compression", lambda: consumed.append(1) or (False, ""))
    monkeypatch.setattr(compat, "peek_compression", lambda: True)

    # mock 组装链：get_context_manager → get_context_for_chat
    cm = MagicMock()

    async def _fake_gcf(exclude_last=True):
        assert exclude_last is False, "R2-A P1-3：闲时直调不得 exclude_last"
        return [{"role": "user", "content": "历史"}]
    cm.get_context_for_chat = _fake_gcf

    async def _fake_get_cm(store):
        return cm

    monkeypatch.setattr("agent.context_manager.get_context_manager", _fake_get_cm)

    async def _fake_store():
        return MagicMock()

    monkeypatch.setattr(compat, "get_message_store", _fake_store)

    runner = MagicMock()
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)

    # mock 受控压缩本体（禁真实 LLM/DB）——真实签名返回 (new_msgs, did)
    with patch("agent.generic.agent_loop.run_controlled_compression") as rcc:
        rcc.return_value = ([], True)
        from niu_api.compat import ChatRequest
        resp = await compat.chat_session(ChatRequest(message="/compact"))

    assert requested == ["manual"]
    assert consumed, "finally 必须 consume 意图（防泄漏进下轮）"
    # run_controlled_compression 经 _run_idle_compression 在 executor 线程被调
    assert rcc.called
    args = rcc.call_args.args
    assert args[2] is runner.client  # client 位置参数
    assert resp.reply
    # 卫生：idle 分支按 plan R8-A P3-2 置闩——测试后释放，防污染其他用例
    from agent.context_assembler import compaction
    compaction.AUTO_GATE.release()


# ---------------------------------------------------------------------------
# 4. agent_loop 响应后不再压实（主 Agent）——行为锁
# ---------------------------------------------------------------------------

def test_response_after_no_compact_main_agent():
    """主 Agent（_is_subagent=False）响应后 usage 达线 → 无响应后压实副作用。

    删除响应后主分支的行为锁：旧行为是此处调 on_context_high_usage 回调压实；
    Task 6 后该参数与响应后压实一并删除——新语义压缩只在发送前门（on_compression_request）。
    9000/10000 = 90% > warningThreshold(70%)，旧代码必然触发响应后压实。
    可观测锁：不置压缩意图 + 不闩 AUTO_GATE（回归若复活响应后压实必现其一）。
    """
    from agent.generic.agent_loop import StepOutcome, agent_runner_loop
    from agent.generic.llmcore import MockResponse
    from agent.context_assembler.compaction import AUTO_GATE
    from agent.compression_intent import peek_compression

    handler = Mock()
    handler._is_subagent = False  # 显式：主 Agent（裸 Mock 属性 truthy 会误判子 Agent）
    handler.max_turns = 1
    handler._done_hooks = []
    handler._current_messages = []
    handler.current_turn = 0
    handler.last_prompt_tokens = 0
    handler._last_prompt_tokens = 0
    handler._last_cached_tokens = None
    handler._is_sync_subagent = False
    handler._bypass_at_prefix = False

    def default_dispatch(tool_name, args, response, index=0):
        if tool_name == "no_tool":
            yield
            return StepOutcome(data=None, next_prompt=None, should_exit=False)
        yield
        return StepOutcome(data="ok", next_prompt="继续", should_exit=False)
    handler.dispatch = default_dispatch
    handler.next_prompt_patcher = lambda np, outcome, turn: np

    usage = type("U", (), {"prompt_tokens": 9000, "completion_tokens": 100})()
    resp = MockResponse(thinking="", content="ok", tool_calls=[], raw=None, usage=usage)
    client = Mock()
    client.last_tools = ""

    def chat(**kwargs):
        def gen():
            yield resp
            return resp
        return gen()
    client.chat = chat

    AUTO_GATE.release()  # 卫生：测试前保证闸门干净
    try:
        events = list(agent_runner_loop(
            client=client, system_prompt="sys", user_input="hi",
            handler=handler, tools_schema=[], max_turns=1, verbose=False,
            context_window_tokens=10000,  # 90% > warningThreshold(70%)
        ))
        assert not peek_compression(), "主 Agent 响应后不得置压缩意图（已迁发送前门）"
        assert not AUTO_GATE._latched, \
            "主 Agent 响应后不得闩 AUTO_GATE（响应后压实已删，回归若复活必现闩锁）"
    finally:
        AUTO_GATE.release()  # 卫生：防污染其他用例


# ---------------------------------------------------------------------------
# 5. 组装出口置 auto 意图（Step 4b）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assembly_exit_sets_auto_intent(tmp_path, monkeypatch):
    """get_context_for_chat 达线 → request_compression('auto') + 返回未压实视图（不就地压实）。"""
    from agent.context_manager import ContextManager
    from agent.session import MessageStore

    store = MessageStore(str(tmp_path / "m.db"))
    await store.init_db()
    await store.add_message(role="user", content="Q" * 2000)
    cm = ContextManager(store, max_tokens=1000, blocks_db_path=tmp_path / "b.db")
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(lambda messages: sum(
                            len(m.get("content", "")) + 8 for m in messages)))

    # 校准倍率钉 1.0（确定性：est=2008 > max_tokens=1000×trigger 必达线，不受真实 ~/.niu 缓存影响）
    import agent.context_assembler.calibration as calibration
    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0

    intents = []
    with patch("agent.compression_intent.request_compression",
               side_effect=lambda r: intents.append(r)), \
         patch("agent.compression_intent.peek_compression", return_value=False):
        from agent.context_assembler.compaction import AUTO_GATE
        AUTO_GATE.release()
        try:
            view = await cm.get_context_for_chat(exclude_last=False)
        finally:
            AUTO_GATE.release()
            calibration._cached_ratio = old_ratio

    assert intents == ["auto"], f"达线应置 auto 意图，实际: {intents}"
    # 不就地压实：视图含原文 Q*2000（未压实组装视图），非 build_compact_view 产物
    assert any(m.get("content") == "Q" * 2000 for m in view), "不得就地压实（意图移交发送前门）"


# ---------------------------------------------------------------------------
# 6. manual 意图 + 提炼失败冷却期：门 defer（不落空转、意图重设）——行为锁
# ---------------------------------------------------------------------------

def test_manual_during_extract_cooldown_defers_no_send_loss(monkeypatch):
    """manual 意图 + _extract_cooldown_active=True → 门 defer（gate_hit=False 落回发送）：
    on_compression_request 不被调、client.chat 仍发生（绝不 continue 空转杀长任务）、
    manual 意图被重新置位（consume 后 (True, 'manual')，下轮冷却过再执行）。"""
    from agent.generic.agent_loop import StepOutcome, agent_runner_loop
    from agent.generic.llmcore import MockResponse

    handler = Mock()
    handler._is_subagent = False  # 显式：主 Agent（裸 Mock 属性 truthy 会误判子 Agent）
    handler.max_turns = 1
    handler._done_hooks = []
    handler._current_messages = []
    handler.current_turn = 0
    handler.last_prompt_tokens = 0
    handler._last_prompt_tokens = 0
    handler._last_cached_tokens = None
    handler._is_sync_subagent = False
    handler._bypass_at_prefix = False

    def default_dispatch(tool_name, args, response, index=0):
        if tool_name == "no_tool":
            yield
            return StepOutcome(data=None, next_prompt=None, should_exit=False)
        yield
        return StepOutcome(data="ok", next_prompt="继续", should_exit=False)
    handler.dispatch = default_dispatch
    handler.next_prompt_patcher = lambda np, outcome, turn: np

    resp = MockResponse(thinking="", content="ok", tool_calls=[], raw=None, usage=None)
    chat_count = []

    def chat(**kwargs):
        chat_count.append(kwargs.get("messages"))

        def gen():
            yield resp
            return resp
        return gen()

    client = Mock()
    client.last_tools = ""
    client.chat = chat

    cb_calls = []

    def _cb(messages, turn):
        cb_calls.append(turn)
        return messages, True

    # 冷却期生效（patch agent_loop 模块内引用的判定函数）
    monkeypatch.setattr("agent.generic.agent_loop._extract_cooldown_active", lambda: True)

    from agent.compression_intent import consume_compression, request_compression
    request_compression("manual")
    try:
        events = list(agent_runner_loop(
            client=client, system_prompt="sys", user_input="hi",
            handler=handler, tools_schema=[], max_turns=1, verbose=False,
            on_compression_request=_cb,
        ))
        assert cb_calls == [], "冷却期 manual 压缩不得执行（on_compression_request 不被调）"
        assert len(chat_count) >= 1, "defer 后必须落回正常发送（绝不 continue 空转杀长任务）"
        had, reason = consume_compression()
        assert (had, reason) == (True, "manual"), \
            f"manual 意图必须被重新置位（下轮冷却过再执行），实际: {(had, reason)}"
    finally:
        # 卫生：清残留意图防污染其他用例
        while consume_compression()[0]:
            pass

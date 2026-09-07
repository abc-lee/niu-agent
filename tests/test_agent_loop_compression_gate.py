# tests/test_agent_loop_compression_gate.py
"""发送前压缩门测试：意图/达线 → 发送前触发 on_compression_request 回调，原 user 指令保持最后一条。"""
from unittest.mock import patch, MagicMock

from agent.compression_intent import peek_compression, reset_compression_intent, request_compression
from agent.generic.agent_loop import agent_runner_loop


class _FakeResp:
    def __init__(self, content="好的", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls  # R3-B P3-2：显式属性（agent_loop 访问 response.tool_calls）
        self.finish_reason = "stop"
        self.usage = None  # 无 usage → 不触发响应后检测路径（被测对象是发送前门）


def _run_loop_with(client, user_input="用户原指令", on_compression_request=None, stop_predicate=None):
    """跑 agent_runner_loop 首轮，捕获每次 client.chat 的 messages。返回 (captured, result)。"""
    captured = []
    orig_chat = client.chat
    def _capturing_chat(messages, tools=None):
        captured.append((list(messages), tools))
        return orig_chat(messages, tools=tools)
    client.chat = _capturing_chat
    handler = MagicMock()
    handler.max_turns = 1
    handler._done_hooks = []
    handler._is_subagent = False
    handler._last_prompt_tokens = 0
    handler._last_cached_tokens = None
    gen = agent_runner_loop(
        client=client,
        system_message={"role": "system", "content": "sys"},
        history=[], user_input=user_input,
        handler=handler, verbose=False, max_turns=1,
        on_compression_request=on_compression_request,
        stop_predicate=stop_predicate,
    )
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        result = e.value
    return captured, result


def test_manual_intent_triggers_compression_callback_before_send():
    """手工意图 → 发送前触发 on_compression_request；回调返回重组消息含完成提示；原指令最后一条。"""
    reset_compression_intent()
    from agent.context_assembler import compaction
    compaction.AUTO_GATE.release()  # R8-A P3-3：复位全局闸门防跨用例闩锁顺序依赖

    def _fake_chat(messages, tools=None):
        def _g():
            yield _FakeResp()
            return _FakeResp()
        return _g()

    client = MagicMock()
    client.chat.side_effect = _fake_chat

    def _on_compression_request(messages, turn, gate_acquired=True):
        # 回调被门调用 → 返回"压缩后"消息：完成提示在原指令前、原 user 指令最后
        # （R4-B P1-2 修正：hint 不能 append 尾部——原指令必须最后一条）
        return list(messages)[:-1] + [{"role": "user", "content": "[系统提示] 上下文压缩已完成。"}] + list(messages)[-1:], True

    request_compression("manual")
    with patch("agent.generic.agent_loop._estimate_usage_ratio", return_value=0.9):
        captured, result = _run_loop_with(client, on_compression_request=_on_compression_request)

    # 门确实在 client.chat 前调用了回调：captured 有 chat 调用，且发送的消息含完成提示
    assert len(captured) == 1
    sent = captured[0][0]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"] == "用户原指令"  # 原指令最后一条
    assert any("[系统提示] 上下文压缩已完成。" in m.get("content", "") for m in sent)


def test_no_intent_no_compression_normal_send():
    """无意图 + 未达线 → 门不触发回调，正常发送。"""
    reset_compression_intent()

    def _fake_chat(messages, tools=None):
        def _g():
            yield _FakeResp()
            return _FakeResp()
        return _g()

    client = MagicMock()
    client.chat.side_effect = _fake_chat
    called = []
    def _on_compression_request(messages, turn, gate_acquired=True):
        called.append(turn)
        return messages, True

    with patch("agent.generic.agent_loop._estimate_usage_ratio", return_value=0.3):
        captured, _ = _run_loop_with(client, on_compression_request=_on_compression_request)
    assert called == []  # 未达线不触发
    assert len(captured) == 1  # 正常发送一次


def test_auto_intent_above_line_triggers_compression():
    """R9-B P2-3：组装出口置 auto 意图 + 估算仍达线 → 门消费执行压缩回调。"""
    reset_compression_intent()
    from agent.context_assembler import compaction
    compaction.AUTO_GATE.release()

    def _fake_chat(messages, tools=None):
        def _g():
            yield _FakeResp()
            return _FakeResp()
        return _g()

    client = MagicMock()
    client.chat.side_effect = _fake_chat
    called = []
    def _on_compression_request(messages, turn, gate_acquired=True):
        called.append(turn)
        return list(messages)[:-1] + [{"role": "user", "content": "[系统提示] 上下文压缩已完成。"}] + list(messages)[-1:], True

    request_compression("auto")
    with patch("agent.generic.agent_loop._estimate_usage_ratio", return_value=0.9), \
         patch("agent.generic.agent_loop._extract_cooldown_active", return_value=False):
        captured, _ = _run_loop_with(client, on_compression_request=_on_compression_request)
    assert called == [1]  # auto 意图 + 达线 → 执行
    sent = captured[0][0]
    assert sent[-1]["content"] == "用户原指令"  # 原指令最后


def test_auto_intent_below_line_skipped():
    """R9-B P2-3：auto 意图但估算已回落 < 触发线 → 放弃（不误压）。"""
    reset_compression_intent()
    from agent.context_assembler import compaction
    compaction.AUTO_GATE.release()

    def _fake_chat(messages, tools=None):
        def _g():
            yield _FakeResp()
            return _FakeResp()
        return _g()

    client = MagicMock()
    client.chat.side_effect = _fake_chat
    called = []
    def _on_compression_request(messages, turn, gate_acquired=True):
        called.append(turn)
        return messages, True

    request_compression("auto")
    with patch("agent.generic.agent_loop._estimate_usage_ratio", return_value=0.3):
        captured, _ = _run_loop_with(client, on_compression_request=_on_compression_request)
    assert called == []  # 回落放弃
    assert len(captured) == 1  # 正常发送


def test_stopped_exit_clears_unconsumed_manual_intent():
    """FinalReview B-P2-1：忙时 /compact 置 manual 意图后 run 未达发送前门即异常退出
    （首轮 stop 检查）→ 意图必须清理，防泄漏到下一会话首轮门被消费（意外压缩）。"""
    reset_compression_intent()

    def _fake_chat(messages, tools=None):
        def _g():
            yield _FakeResp()
            return _FakeResp()
        return _g()

    client = MagicMock()
    client.chat.side_effect = _fake_chat
    request_compression("manual")
    captured, result = _run_loop_with(
        client, stop_predicate=lambda: True,  # 首轮即停——门（consume）从未到达
        on_compression_request=lambda m, t, g=True: (m, True),
    )
    assert result["result"] == "STOPPED"
    assert captured == []  # 未达 client.chat（门在 chat 前，stop 更早）
    assert peek_compression() is False, "异常退出必须清未消费意图（防下会话首轮意外压缩）"


def test_normal_exit_keeps_unconsumed_manual_intent():
    """FinalReview B-P2-1 语义对侧：正常收尾不清意图——manual 冷却 defer 重设后任务
    自然完成 → 意图顺延到下一条消息首轮门（用户按了 /compact 就该压，"任务间隙执行"）。"""
    reset_compression_intent()

    def _fake_chat(messages, tools=None):
        def _g():
            yield _FakeResp()
            return _FakeResp()
        return _g()

    client = MagicMock()
    client.chat.side_effect = _fake_chat
    cb_calls = []
    request_compression("manual")
    try:
        with patch("agent.generic.agent_loop._estimate_usage_ratio", return_value=0.9), \
             patch("agent.generic.agent_loop._extract_cooldown_active", return_value=True):
            captured, result = _run_loop_with(
                client,
                on_compression_request=lambda m, t, g=True: (cb_calls.append(t), (m, True))[1],
            )
        assert cb_calls == []  # 冷却期不执行压缩（defer）
        assert len(captured) == 1  # defer 后落回正常发送
        assert result["result"] in ("CURRENT_TASK_DONE", "MAX_TURNS_EXCEEDED")
        assert peek_compression() is True, "正常出口不清意图——顺延到下一条消息首轮门（设计内）"
    finally:
        reset_compression_intent()  # 卫生：本用例断言的就是残留意图，必须清防污染后续文件

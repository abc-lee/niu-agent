"""Tests for the supplement queue mechanism (见缝插针)."""
import threading


def test_supplement_queue_initially_empty():
    """Supplement queue starts empty."""
    from agent.runner import drain_supplements
    assert drain_supplements() == []


def test_enqueue_supplement_adds_message():
    """enqueue_supplement adds a message to the queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    enqueue_supplement("用户补充的信息")
    result = drain_supplements()
    assert len(result) == 1
    assert result[0] == "用户补充的信息"


def test_drain_supplements_empties_queue():
    """drain_supplements removes all messages from the queue."""
    from agent.runner import enqueue_supplement, drain_supplements
    enqueue_supplement("消息1")
    enqueue_supplement("消息2")
    assert len(drain_supplements()) == 2
    assert drain_supplements() == []


def test_supplement_queue_thread_safe():
    """enqueue_supplement and drain_supplements are thread-safe."""
    from agent.runner import enqueue_supplement, drain_supplements
    errors = []

    def enqueue_many():
        try:
            for i in range(100):
                enqueue_supplement(f"msg-{i}")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=enqueue_many)
    t2 = threading.Thread(target=enqueue_many)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    all_msgs = []
    while True:
        batch = drain_supplements()
        if not batch:
            break
        all_msgs.extend(batch)

    assert len(all_msgs) == 200
    assert len(errors) == 0


def test_drain_supplements_no_race_condition():
    """drain_supplements uses get_nowait() without empty() check — no race."""
    from agent.runner import enqueue_supplement, drain_supplements
    for i in range(50):
        enqueue_supplement(f"race-{i}")
    result = drain_supplements()
    assert len(result) == 50
    assert drain_supplements() == []


def test_drain_supplement_empty_returns_none():
    """No pending messages returns None."""
    from agent.runner import drain_supplement
    assert drain_supplement() is None


def test_drain_supplement_single_returns_raw():
    """Single pending message returned as-is."""
    from agent.runner import enqueue_supplement, drain_supplement
    enqueue_supplement("只有一条补充")
    assert drain_supplement() == "只有一条补充"


def test_drain_supplement_multiple_joins_with_prefix():
    """Multiple pending messages joined with [补充] prefix."""
    from agent.runner import enqueue_supplement, drain_supplement
    enqueue_supplement("第一个补充")
    enqueue_supplement("第二个补充")
    result = drain_supplement()
    assert "[补充] 第一个补充" in result
    assert "[补充] 第二个补充" in result


def test_supplement_inserted_before_next_prompt():
    """Supplement messages appear before next_prompt in messages sent to LLM."""
    from agent.runner import enqueue_supplement, drain_supplements
    from agent.generic.agent_loop import agent_runner_loop, StreamEvent
    from unittest.mock import MagicMock

    drain_supplements()

    turn = 0
    captured_messages_list = []

    # Build a realistic tool_call mock with proper string attributes for json.dumps
    tc1 = MagicMock()
    tc1.id = "tc1"
    tc1.function.name = "test_tool"
    tc1.function.arguments = "{}"

    def mock_chat(**kwargs):
        nonlocal turn
        turn += 1
        captured_messages_list.append(list(kwargs.get("messages", [])))

        resp = MagicMock()
        if turn == 1:
            resp.tool_calls = [tc1]
            resp.content = ""
            # 用户在工具执行期间发送补充消息
            enqueue_supplement("中途补充的信息")
        else:
            resp.tool_calls = None
            resp.content = "好的，已处理"
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        yield resp
        return resp

    client = MagicMock()
    client.chat = mock_chat

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    handler.next_prompt_patcher = lambda np, _ctx, tn: np

    def mock_dispatch(tool_name, args, resp, index=0):
        outcome = MagicMock()
        outcome.should_exit = False
        outcome.data = {"status": "ok"}
        outcome.next_prompt = ""
        yield StreamEvent("tool_marker", f"tool: {tool_name}")
        return outcome

    handler.dispatch = mock_dispatch

    list(agent_runner_loop(
        client=client, system_prompt="test", user_input="hello",
        handler=handler, tools_schema=[], max_turns=5,
    ))

    # 第二轮的 messages 中应该包含补充消息
    assert len(captured_messages_list) >= 2
    second_call_messages = captured_messages_list[1]
    user_msgs = [m for m in second_call_messages if m.get("role") == "user"]
    # 补充消息应该出现在最后一条 user 消息中
    last_user = user_msgs[-1] if user_msgs else None
    assert last_user is not None, f"No user messages found in: {user_msgs}"
    content = last_user["content"]
    assert "中途补充的信息" in content, f"Supplement not found in last user message: {content}"

"""Tests for stop flag mechanism."""
import threading
import time

from agent.runner import clear_stop, is_stop_requested, request_stop


def test_initial_state_is_not_stopped():
    """Stop flag should be clear initially."""
    clear_stop()
    assert is_stop_requested() is False


def test_request_stop_sets_flag():
    """request_stop() should set the flag."""
    clear_stop()
    request_stop()
    assert is_stop_requested() is True


def test_clear_stop_resets_flag():
    """clear_stop() should reset the flag."""
    request_stop()
    clear_stop()
    assert is_stop_requested() is False


def test_stop_flag_is_thread_safe():
    """Stop flag should be thread-safe."""
    clear_stop()
    results = []

    def set_flag():
        time.sleep(0.01)
        request_stop()
        results.append("set")

    t = threading.Thread(target=set_flag)
    t.start()
    # Spin until flag is set
    while not is_stop_requested():
        time.sleep(0.001)
    results.append("seen")
    t.join()
    assert results == ["set", "seen"]


def test_stop_flag_checked_in_loop():
    """agent_runner_loop should exit when stop flag is set."""
    from unittest.mock import MagicMock

    from agent.generic.agent_loop import StreamEvent, agent_runner_loop
    from agent.runner import clear_stop, request_stop

    clear_stop()

    client = MagicMock()
    response = MagicMock()
    response.tool_calls = None
    response.content = "Hello"
    response.usage = MagicMock(input_tokens=10, output_tokens=5)

    call_count = 0

    def chat_with_stop_check(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: return a response with a tool call
            resp = MagicMock()
            tc = MagicMock()
            tc.function.name = "test_tool"
            tc.function.arguments = "{}"
            tc.id = "call_123"
            resp.tool_calls = [tc]
            resp.content = ""
            resp.usage = MagicMock(input_tokens=10, output_tokens=5)
            yield resp
            return resp
        else:
            yield response
            return response

    client.chat = chat_with_stop_check

    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []

    def mock_dispatch(tool_name, args, resp, index=0):
        request_stop()
        outcome = MagicMock()
        outcome.should_exit = False
        outcome.data = {"status": "ok"}
        outcome.next_prompt = ""
        yield StreamEvent("tool_marker", f"tool: {tool_name}")
        return outcome

    handler.dispatch = mock_dispatch

    events = list(agent_runner_loop(
        client=client,
        system_prompt="test",
        user_input="hello",
        handler=handler,
        tools_schema=[],
        max_turns=5,
    ))

    idle_events = [e for e in events if isinstance(e, StreamEvent) and e.type == "system" and e.content == "chat_idle"]
    assert len(idle_events) >= 1, "Loop should have exited with chat_idle"

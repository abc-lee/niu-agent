
import pytest


def test_stream_event_creation():
    from agent.generic.agent_loop import StreamEvent

    ev = StreamEvent(type="reply", content="hello")
    assert ev.type == "reply"
    assert ev.content == "hello"


def test_stream_event_str_returns_content():
    from agent.generic.agent_loop import StreamEvent

    ev = StreamEvent(type="reply", content="world")
    assert str(ev) == "world"


def test_stream_event_repr():
    from agent.generic.agent_loop import StreamEvent

    ev = StreamEvent(type="tool_marker", content="calling tool")
    assert "StreamEvent" in repr(ev)
    assert "tool_marker" in repr(ev)
    assert "calling tool" in repr(ev)


def test_stream_event_valid_types():
    from agent.generic.agent_loop import StreamEvent

    for t in ("reply", "tool_marker", "system"):
        ev = StreamEvent(type=t, content="x")
        assert ev.type == t


def test_stream_event_invalid_type():
    from agent.generic.agent_loop import StreamEvent

    with pytest.raises(ValueError):
        StreamEvent(type="invalid", content="x")


def test_stream_event_empty_content():
    from agent.generic.agent_loop import StreamEvent

    ev = StreamEvent(type="reply", content="")
    assert ev.content == ""
    assert str(ev) == ""


def test_stream_event_str_backward_compat():
    """Existing code does: for chunk in gen: result += chunk
    StreamEvent must behave like str in concatenation."""
    from agent.generic.agent_loop import StreamEvent

    ev = StreamEvent(type="reply", content="hello ")
    result = ev + "world"
    assert result == "hello world"

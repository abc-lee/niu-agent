"""
Integration test for disk mode tool dispatch.

Tests that handler dispatch works correctly in disk mode (no tool_lifecycle).
"""

from unittest.mock import Mock

import pytest

from agent.generic.agent_loop import StepOutcome
from agent.handler import NiuHandler
from agent.runner import NiuRunner


@pytest.fixture
def clean_runner():
    """Ensure clean global runner state"""
    import agent.runner as runner_module
    runner_module._runner = None
    yield
    runner_module._runner = None


def test_tool_dispatch_without_tool_lifecycle(clean_runner):
    """Test that tool dispatch works in disk mode without tool_lifecycle."""

    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)
    handler = NiuHandler(mcp_client=None)

    # Verify runner has no tool_lifecycle attribute
    assert not hasattr(runner, 'tool_lifecycle'), \
        "Runner should not have tool_lifecycle in disk mode"

    # Mock tool registry to return a mock function
    from agent.tool_registry import get_registry
    registry = get_registry()
    mock_func = Mock(return_value={"status": "success", "data": "test result"})
    registry._tools["test-server/test-tool"] = mock_func
    mock_func._mcp_server = None

    # Register the global runner so handler.dispatch() can access it
    import agent.runner as runner_module
    runner_module._runner = runner

    try:
        # Execute tool via dispatch
        gen = handler.dispatch("test-server/test-tool", {"arg": "value"}, Mock())

        # Consume the generator
        results = []
        try:
            while True:
                results.append(next(gen))
        except StopIteration as e:
            final_result = e.value

        # Verify tool was actually executed
        mock_func.assert_called_once_with(arg="value")

        # Verify tool execution result
        assert isinstance(final_result, StepOutcome), \
            f"Expected StepOutcome, got {type(final_result)}"
        assert final_result.data == {"status": "success", "data": "test result"}, \
            f"Expected mock result, got {final_result.data}"
    finally:
        if "test-server/test-tool" in registry._tools:
            del registry._tools["test-server/test-tool"]


def test_handler_no_tool_lifecycle_reference():
    """Verify handler.py source code has no tool_lifecycle references."""
    import agent.handler as handler_module
    source = open(handler_module.__file__, encoding="utf-8").read()
    assert "tool_lifecycle" not in source, \
        "handler.py should not reference tool_lifecycle"


def test_runner_no_tool_lifecycle_reference():
    """Verify runner.py source code has no tool_lifecycle references."""
    import agent.runner as runner_module
    source = open(runner_module.__file__, encoding="utf-8").read()
    assert "tool_lifecycle" not in source, \
        "runner.py should not reference tool_lifecycle"

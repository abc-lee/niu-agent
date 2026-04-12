"""
Integration test for tool hit recording

Tests that tools are only hit when actually executed, not when found via vector search.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from agent.runner import NiuRunner, get_runner
from agent.handler import NiuHandler
from agent.generic.agent_loop import StepOutcome


@pytest.fixture
def temp_storage(tmp_path):
    """Use temporary directory for test storage"""
    original_home = Path.home()
    temp_home = tmp_path / "home"
    temp_home.mkdir(parents=True)

    original_home_method = Path.home
    Path.home = lambda: temp_home

    yield temp_home

    Path.home = original_home_method


@pytest.fixture
def clean_runner():
    """Ensure clean global runner state"""
    import agent.runner as runner_module
    runner_module._runner = None
    yield
    runner_module._runner = None


def test_tool_hit_on_execution(temp_storage, clean_runner):
    """Test that tool is hit when actually executed, not on vector search"""

    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    # Create runner instance
    runner = NiuRunner(llm_config=llm_config, mcp_client=None)
    handler = NiuHandler(mcp_client=None)

    # Mock tool registry to return a mock function
    from agent.tool_registry import get_registry
    registry = get_registry()
    mock_func = Mock(return_value={"status": "success", "data": "test result"})
    registry._tools["test-server/test-tool"] = mock_func

    # Ensure the mock has no _mcp_server attribute to avoid errors
    mock_func._mcp_server = None

    # Register the global runner so handler.dispatch() can access it
    import agent.runner as runner_module
    runner_module._runner = runner

    # CRITICAL FIX 3: Use try/finally to ensure cleanup always happens
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

        # CRITICAL FIX 1: Verify tool was actually executed
        mock_func.assert_called_once_with(arg="value")

        # CRITICAL FIX 2: Verify tool execution result
        assert isinstance(final_result, StepOutcome), f"Expected StepOutcome, got {type(final_result)}"
        assert final_result.data == {"status": "success", "data": "test result"}, f"Expected mock result, got {final_result.data}"

        # Verify tool was hit
        score = runner.tool_lifecycle.get_tool_score("test-server/test-tool")
        assert score == 100, f"Expected score 100, got {score}"

        # Verify persistence
        scores_file = temp_storage / ".niu" / "tool_scores.json"
        assert scores_file.exists(), "Scores file should exist"

        scores = json.loads(scores_file.read_text(encoding="utf-8"))
        assert scores["test-server/test-tool"] == 100, f"Expected persisted score 100, got {scores.get('test-server/test-tool')}"
    finally:
        # Cleanup: Always remove test tool from registry
        if "test-server/test-tool" in registry._tools:
            del registry._tools["test-server/test-tool"]



def test_no_hit_on_vector_search(temp_storage, clean_runner):
    """Test that vector search does NOT trigger hit recording"""

    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Mock vector search to return a tool
    mock_result = Mock()
    mock_result.metadata = {
        "name": "test-tool",
        "server": "test-server",
        "category": "mcp_tool",
        "level": "l1"
    }
    mock_result.score = 0.85
    mock_result.content = "Test tool description"

    with patch.object(runner.vector_search, 'search') as mock_search:
        mock_search.return_value = [mock_result]

        # Call _inject_dynamic_resources (this should NOT record a hit)
        injection = runner._inject_dynamic_resources("test query")

    # Verify tool was NOT hit (because it wasn't executed)
    score = runner.tool_lifecycle.get_tool_score("test-server/test-tool")
    assert score == 0, f"Expected score 0 (not hit), got {score}"

    # Verify no persistence file created
    scores_file = temp_storage / ".niu" / "tool_scores.json"
    if scores_file.exists():
        scores = json.loads(scores_file.read_text(encoding="utf-8"))
        assert "test-server/test-tool" not in scores, "Tool should not be in scores file"


def test_hit_recording_location_correctness(temp_storage, clean_runner):
    """Test that hit recording happens in handler.dispatch(), not in other places"""

    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Test 1: chat() method should not hit tools just by calling it
    # (We won't actually run the full chat loop, just check that tool_lifecycle
    # is not being modified during setup)

    initial_active_tools = runner.tool_lifecycle.get_active_tools()
    assert len(initial_active_tools) == 0, "Should start with no active tools"

    # Test 2: _inject_dynamic_resources should not hit tools
    mock_result = Mock()
    mock_result.metadata = {
        "name": "another-tool",
        "server": "another-server",
        "category": "mcp_tool",
        "level": "l1"
    }
    mock_result.score = 0.75
    mock_result.content = "Another tool description"

    with patch.object(runner.vector_search, 'search') as mock_search:
        with patch.object(runner.vector_search, 'search_interaction_habits') as mock_habits:
            mock_search.return_value = [mock_result]
            mock_habits.return_value = []

            injection = runner._inject_dynamic_resources("another query")

    # Verify no tools were hit
    active_tools = runner.tool_lifecycle.get_active_tools()
    assert len(active_tools) == 0, f"Expected 0 active tools after vector search, got {active_tools}"

    score = runner.tool_lifecycle.get_tool_score("another-server/another-tool")
    assert score == 0, "Tool should not be hit by vector search"

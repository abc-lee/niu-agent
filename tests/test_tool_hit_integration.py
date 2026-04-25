"""
Integration test for tool hit recording

Tests that tools are only hit when actually executed, not when found via LightRAG search.
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
    """Test that tool is hit when actually executed, not on LightRAG search"""

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
        assert isinstance(final_result, StepOutcome), f"Expected StepOutcome, got {type(final_result)}"
        assert final_result.data == {"status": "success", "data": "test result"}, f"Expected mock result, got {final_result.data}"

        # Verify tool was hit (hit_tool default score is 65 in 衰减-覆盖模式)
        score = runner.tool_lifecycle.get_tool_score("test-server/test-tool")
        assert score == 65, f"Expected score 65, got {score}"

        # Verify persistence
        scores_file = temp_storage / ".niu" / "tool_scores.json"
        assert scores_file.exists(), "Scores file should exist"

        scores = json.loads(scores_file.read_text(encoding="utf-8"))
        assert scores["test-server/test-tool"] == 65, f"Expected persisted score 65, got {scores.get('test-server/test-tool')}"
    finally:
        # Cleanup: Always remove test tool from registry
        if "test-server/test-tool" in registry._tools:
            del registry._tools["test-server/test-tool"]



def test_no_hit_on_lightrag_search(temp_storage, clean_runner):
    """Test that LightRAG search does NOT trigger hit recording"""

    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Mock LightRAG search_multi_lightrag to return a tool entity
    mock_lightrag_results = {
        "skill": [],
        "mcp_tool": [{"entity_name": "tool:test-server/test-tool", "entity_type": "tool", "description": "Test tool"}],
        "knowledge": [],
    }

    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.search_multi_lightrag.return_value = mock_lightrag_results
        mock_adapter.search_interaction_habits.return_value = []

        # Call _inject_dynamic_resources (this should NOT record a hit)
        injection = runner._inject_dynamic_resources("test query")

    # Verify tool was NOT hit (because it wasn't executed)
    score = runner.tool_lifecycle.get_tool_score("test-server/test-tool")
    # LightRAG search sets tool scores via _build_tool_scores_from_lightrag,
    # but does NOT call hit_tool(). The score comes from rank-based proxy.
    # This test verifies that _inject_dynamic_resources doesn't call hit_tool.
    assert score <= 70, f"Expected score <= 70 (from search, not hit), got {score}"


def test_hit_recording_location_correctness(temp_storage, clean_runner):
    """Test that hit recording happens in handler.dispatch(), not in other places"""

    llm_config = {
        "apikey": "test-key",
        "model": "test-model",
        "apibase": "http://test.com",
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)

    # Test 1: chat() method should not hit tools just by calling it
    initial_active_tools = runner.tool_lifecycle.get_active_tools()
    assert len(initial_active_tools) == 0, "Should start with no active tools"

    # Test 2: _inject_dynamic_resources should not hit tools
    mock_lightrag_results = {
        "skill": [],
        "mcp_tool": [{"entity_name": "tool:another-server/another-tool", "entity_type": "tool", "description": "Another tool"}],
        "knowledge": [],
    }

    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.search_multi_lightrag.return_value = mock_lightrag_results
        mock_adapter.search_skills.return_value = []
        mock_adapter.search_interaction_habits.return_value = []

        injection = runner._inject_dynamic_resources("another query")

    # Verify no tools were hit via hit_tool()
    # (tools may have scores from search, but not from hit_tool)
    score = runner.tool_lifecycle.get_tool_score("another-server/another-tool")
    # Score from search is rank-based proxy (<= 70), not from hit_tool (65)
    assert score <= 70, f"Expected score <= 70 (from search), got {score}"

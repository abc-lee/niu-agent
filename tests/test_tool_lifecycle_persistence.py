import json
import tempfile
from pathlib import Path

import pytest

from agent.tool_lifecycle import ToolLifecycleManager


@pytest.fixture
def temp_storage(tmp_path):
    """Use temporary directory for tool scores"""
    original_home = Path.home()
    temp_home = tmp_path / "home"
    temp_home.mkdir(parents=True)

    original_home_method = Path.home
    Path.home = lambda: temp_home

    yield temp_home

    Path.home = original_home_method


def test_tool_score_persistence(temp_storage):
    """Test that tool scores persist to JSON file"""
    manager = ToolLifecycleManager()
    manager.hit_tool("browser-server/browser_navigate")

    # Check file exists
    scores_file = temp_storage / ".niu" / "tool_scores.json"
    assert scores_file.exists()

    # Check content
    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    assert scores["browser-server/browser_navigate"] == 100


def test_persistence_across_instances(temp_storage):
    """Test that scores persist across manager instances"""
    # First instance
    manager1 = ToolLifecycleManager()
    manager1.hit_tool("test-server/test-tool")

    # Second instance (should load from file)
    manager2 = ToolLifecycleManager()
    assert manager2.get_tool_score("test-server/test-tool") == 100


def test_decay_saves_to_file(temp_storage):
    """Test that decay updates the file"""
    manager = ToolLifecycleManager()
    manager.hit_tool("test-server/test-tool")

    manager.decay_tools()

    # Check file updated
    scores_file = temp_storage / ".niu" / "tool_scores.json"
    scores = json.loads(scores_file.read_text(encoding="utf-8"))
    assert scores["test-server/test-tool"] == 90

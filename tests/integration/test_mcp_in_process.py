"""
Integration tests for MCP in-process architecture.

Tests the ToolRegistry and MCP loader to ensure tools can be loaded
and called directly without stdio communication overhead.
"""

import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Add mcp-servers to sys.path so we can import modules
# This simulates the workdir configuration in mcp-servers.yaml
PHOTO_SERVER_SRC = Path(__file__).parent.parent.parent / "mcp-servers" / "photo-server" / "src"
if str(PHOTO_SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(PHOTO_SERVER_SRC))


def test_prerequisites():
    """Verify that all required components exist."""
    from agent.mcp_loader import load_mcp_tools
    from agent.tool_registry import ToolRegistry, get_registry, reset_registry

    # Test ToolRegistry
    registry = ToolRegistry()
    assert registry is not None

    # Test global registry
    reset_registry()
    registry = get_registry()
    assert registry is not None

    # Test MCP loader exists
    assert callable(load_mcp_tools)


def test_load_mcp_tools():
    """Test loading MCP tools into ToolRegistry."""
    from agent.mcp_loader import load_mcp_tools
    from agent.tool_registry import reset_registry

    # Reset registry to ensure clean state
    reset_registry()

    # Load only photo-server for testing
    registry = load_mcp_tools(
        required_servers=[
            ("photo-server", "niu_photo_server"),
        ]
    )

    # Verify registry is populated
    assert registry is not None
    tools = registry.list_tools()
    assert len(tools) > 0

    # Check specific tools exist (updated tool names after refactor)
    assert registry.has_tool("photo-server/ingest")
    assert registry.has_tool("photo-server/unload_face_model")


def test_document_ingestion_via_direct_call(tmp_path):
    """Test document ingestion via direct function call.

    photo-server/ingest routes through async call_tool(), so we test
    the underlying ingest_document() function directly instead.
    """
    # Set up isolated database path
    db_path = tmp_path / "test.db"
    os.environ["NIU_DB_PATH"] = str(db_path)

    storage_path = tmp_path / "storage"
    storage_path.mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_PATH"] = str(storage_path)

    # Create a simple test markdown file
    test_file = tmp_path / "test.md"
    test_file.write_text(
        "# Test Document\n\nThis is a test document for integration testing.\n",
        encoding="utf-8",
    )

    # Direct call to ingest_document (synchronous)
    from niu_photo_server import ingest_document
    result = ingest_document(
        file_path=str(test_file), category="其他", mode="copy"
    )

    # Verify ingestion result
    assert result is not None
    assert "status" in result

    # Clean up environment
    del os.environ["NIU_DB_PATH"]
    del os.environ["WORKSPACE_PATH"]


def test_photo_ingestion_skipped():
    """Photo ingestion test is skipped due to InsightFace complexity."""
    pytest.skip("Photo ingestion test skipped - InsightFace complexity")


def test_performance_benchmark(tmp_path):
    """Test that in-process tool calls are fast (< 1 second for 10 calls).

    This validates that the in-process architecture eliminates stdio overhead.

    Expected:
    - In-process: 10 calls < 1 second (~0.1s each)
    - Stdio mode: 10 calls ~40 seconds (~4s each)

    Args:
        tmp_path: Pytest fixture providing a temporary directory
    """
    from agent.mcp_loader import load_mcp_tools
    from agent.tool_registry import reset_registry

    # Setup: Load tools
    reset_registry()
    registry = load_mcp_tools(
        required_servers=[
            ("photo-server", "niu_photo_server"),
        ]
    )

    # Set up isolated database path
    db_path = tmp_path / "test.db"
    os.environ["NIU_DB_PATH"] = str(db_path)

    # Get a simple tool that doesn't require heavy processing
    # unload_face_model is a module-level function, so ToolRegistry
    # resolves it directly (not through async call_tool wrapper)
    tool = registry.get("photo-server/unload_face_model")
    assert tool is not None, "unload_face_model tool not found"

    # Warm up (first call might have some initialization overhead)
    tool()

    # Benchmark: 10 calls
    start_time = time.time()
    for _ in range(10):
        result = tool()
        assert result["status"] == "success"

    elapsed = time.time() - start_time

    # Verify performance
    assert elapsed < 1.0, f"Performance test failed: {elapsed:.3f}s > 1.0s (should be < 0.1s per call)"

    # Log performance metrics
    avg_time = elapsed / 10
    print(f"\nPerformance: {elapsed:.3f}s total, {avg_time:.4f}s per call")
    print(f"Speedup vs stdio: ~{40 / elapsed:.1f}x faster")

    # Clean up environment
    del os.environ["NIU_DB_PATH"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

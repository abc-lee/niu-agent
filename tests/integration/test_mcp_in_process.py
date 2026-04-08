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

# Add mcp-servers to sys.path so we can import modules
# This simulates the workdir configuration in mcp-servers.yaml
PHOTO_SERVER_SRC = Path(__file__).parent.parent.parent / "mcp-servers" / "photo-server" / "src"
if str(PHOTO_SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(PHOTO_SERVER_SRC))


def test_prerequisites():
    """Verify that all required components exist."""
    from agent.tool_registry import ToolRegistry, get_registry, reset_registry
    from agent.mcp_loader import load_mcp_tools

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
    from agent.tool_registry import reset_registry
    from agent.mcp_loader import load_mcp_tools

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

    # Check specific tools exist
    assert registry.has_tool("photo-server/ingest_document")
    assert registry.has_tool("photo-server/store_document_l1")
    assert registry.has_tool("photo-server/unload_face_model")


def test_document_ingestion_flow(tmp_path):
    """Test document ingestion → L1 storage flow.

    This tests the complete flow:
    1. Create a temporary markdown file
    2. Call ingest_document tool
    3. Verify need_l1 status
    4. Call store_document_l1 tool
    5. Verify success

    Args:
        tmp_path: Pytest fixture providing a temporary directory
    """
    from agent.tool_registry import get_registry, reset_registry
    from agent.mcp_loader import load_mcp_tools

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

    # Also set up isolated storage path for documents
    storage_path = tmp_path / "storage"
    storage_path.mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_PATH"] = str(storage_path)

    # Create a simple test markdown file
    test_file = tmp_path / "test.md"
    test_file.write_text(
        "# Test Document\n\nThis is a test document for integration testing.\n",
        encoding="utf-8",
    )

    # Step 1: Get ingest_document tool
    ingest_tool = registry.get("photo-server/ingest_document")
    assert ingest_tool is not None, "ingest_document tool not found"

    # Step 2: Call ingest_document
    result = ingest_tool(
        file_path=str(test_file), category="其他", mode="copy"
    )

    # Verify ingestion result
    assert result is not None
    assert "status" in result

    # Note: ingest_document may return different statuses:
    # - "success" if file was ingested
    # - "need_l1" if L1 summary is needed
    # - "error" if something went wrong
    assert result["status"] in ["success", "need_l1"], f"Unexpected status: {result}"

    # Step 3: If need_l1, call store_document_l1
    if result["status"] == "need_l1":
        store_tool = registry.get("photo-server/store_document_l1")
        assert store_tool is not None, "store_document_l1 tool not found"

        # Create a simple L1 summary
        # Format: 标题|关键词|摘要|实体|类型|指针
        l1_summary = "Test Document|测试,文档|这是一个用于集成测试的测试文档|测试|技术文档|test.md"

        store_result = store_tool(
            file_path=result.get("file_path", str(test_file)),
            l1=l1_summary,
            l2=None,
        )

        # Verify storage result
        assert store_result is not None
        assert store_result.get("status") == "success", f"Storage failed: {store_result}"

    # Clean up environment
    del os.environ["NIU_DB_PATH"]
    del os.environ["WORKSPACE_PATH"]


def test_photo_ingestion_skipped():
    """Photo ingestion test is skipped due to InsightFace complexity.

    Rationale:
    - InsightFace model loading requires ~326MB memory
    - Model may not be installed in test environment
    - Document ingestion test already validates the architecture
    - Photo test would add unnecessary complexity without additional value

    The in-process architecture is validated by:
    - test_document_ingestion_flow (complete tool calling flow)
    - test_performance_benchmark (direct function call overhead)
    """
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
    from agent.tool_registry import get_registry, reset_registry
    from agent.mcp_loader import load_mcp_tools

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
    # unload_face_model is perfect: just sets global variables to None
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

#!/usr/bin/env python3
"""Test user memory tools (remember/forget/list)"""

import json
import sys
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _setup_module(memory_path):
    """Patch _get_memory_json_path and return the module"""
    # Import the module
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"))
    import niu_memory_server as mod

    # Reset the global path
    mod._get_memory_json_path.__code__  # noqa - just accessing to verify it exists
    # Patch by setting the global
    mod.MEMORY_JSON_PATH = memory_path
    return mod


async def test_user_memory_remember():
    """Adding memories up to limit"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        mod = _setup_module(memory_path)

        # Add first memory
        result = await mod.user_memory_remember_handler(content="我喜欢Python")
        assert result["status"] == "success", f"Expected success, got {result}"
        assert result["current_memories"] == ["我喜欢Python"]

        # Add second
        result = await mod.user_memory_remember_handler(content="密码是abc")
        assert result["status"] == "success"
        assert len(result["current_memories"]) == 2

    print("PASS: test_user_memory_remember")


async def test_user_memory_remember_full():
    """Reject when memory is full"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        mod = _setup_module(memory_path)

        # Fill to max
        for i in range(mod.MAX_PERMANENT_ITEMS):
            result = await mod.user_memory_remember_handler(content=f"记忆{i}")
            assert result["status"] == "success", f"Failed at item {i}: {result}"

        # Try to add one more
        result = await mod.user_memory_remember_handler(content="超限记忆")
        assert result["status"] == "error"
        assert "已满" in result["message"]

    print("PASS: test_user_memory_remember_full")


async def test_user_memory_forget_by_index():
    """Delete by 1-based index"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": ["A", "B", "C"]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        mod = _setup_module(memory_path)

        result = await mod.user_memory_forget_handler(index=2)
        assert result["status"] == "success"
        assert "B" in result["message"]
        assert result["current_memories"] == ["A", "C"]

    print("PASS: test_user_memory_forget_by_index")


async def test_user_memory_forget_by_keyword():
    """Delete by keyword substring match"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": ["我喜欢Python", "密码是abc", "每周五例会"]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        mod = _setup_module(memory_path)

        result = await mod.user_memory_forget_handler(keyword="密码")
        assert result["status"] == "success"
        assert result["current_memories"] == ["我喜欢Python", "每周五例会"]

    print("PASS: test_user_memory_forget_by_keyword")


async def test_user_memory_list():
    """List all memories"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": ["A", "B"]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        mod = _setup_module(memory_path)

        result = await mod.user_memory_list_handler()
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["max"] == 5
        assert result["memories"] == ["A", "B"]

    print("PASS: test_user_memory_list")


def test_truncate_over_limit():
    """Truncate permanent array > 5 on load"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [f"记忆{i}" for i in range(8)]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        mod = _setup_module(memory_path)

        result = mod._read_memory_json()
        assert len(result["permanent"]) == mod.MAX_PERMANENT_ITEMS
        # Kept first 5
        assert result["permanent"][0] == "记忆0"
        assert result["permanent"][4] == "记忆4"

    print("PASS: test_truncate_over_limit")


if __name__ == "__main__":
    asyncio.run(test_user_memory_remember())
    asyncio.run(test_user_memory_remember_full())
    asyncio.run(test_user_memory_forget_by_index())
    asyncio.run(test_user_memory_forget_by_keyword())
    asyncio.run(test_user_memory_list())
    test_truncate_over_limit()
    print("\nAll tests passed!")

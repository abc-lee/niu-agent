#!/usr/bin/env python3
"""Test user memory tools (remember/forget/list) with dict format"""

import json
import sys
import asyncio
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"))

import niu_memory_server as mod


def _setup_module(memory_path):
    """Patch MEMORY_JSON_PATH for test isolation"""
    mod._reset_memory_json_path()
    mod.MEMORY_JSON_PATH = memory_path
    return mod


def _mem(content, type="memory"):
    """Helper to create a permanent item dict"""
    return {"type": type, "content": content}


async def test_user_memory_remember():
    """Adding memories up to limit"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Add first memory
        result = await mod.user_memory_remember_handler(content="我喜欢Python")
        assert result["status"] == "success", f"Expected success, got {result}"
        assert result["current_memories"] == [_mem("我喜欢Python")]

        # Add second
        result = await mod.user_memory_remember_handler(content="密码是abc")
        assert result["status"] == "success"
        assert len(result["current_memories"]) == 2

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_remember")


async def test_user_memory_remember_full():
    """Reject when memory is full"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Fill to max (4 memory items)
        for i in range(mod.MAX_MEMORY_ITEMS):
            result = await mod.user_memory_remember_handler(content=f"记忆{i}")
            assert result["status"] == "success", f"Failed at item {i}: {result}"

        # Try to add one more
        result = await mod.user_memory_remember_handler(content="超限记忆")
        assert result["status"] == "error"
        assert "已满" in result["message"]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_remember_full")


async def test_task_type():
    """Task type: auto-replace when slot is full"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Add first task
        result = await mod.user_memory_remember_handler(content="修复登录bug", type="task")
        assert result["status"] == "success"
        assert result["current_memories"] == [_mem("修复登录bug", "task")]

        # Add second task — should auto-replace first
        result = await mod.user_memory_remember_handler(content="重构数据库", type="task")
        assert result["status"] == "success"
        assert result["current_memories"] == [_mem("重构数据库", "task")]
        assert "覆盖" in result["message"]

        # Add a memory item alongside task
        result = await mod.user_memory_remember_handler(content="我喜欢Python", type="memory")
        assert result["status"] == "success"
        assert len(result["current_memories"]) == 2

    mod._reset_memory_json_path()
    print("PASS: test_task_type")


async def test_user_memory_forget_by_index():
    """Delete by 1-based index"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("A"), _mem("B"), _mem("C")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = await mod.user_memory_forget_handler(index=2)
        assert result["status"] == "success"
        assert "B" in result["message"]
        assert result["current_memories"] == [_mem("A"), _mem("C")]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_forget_by_index")


async def test_user_memory_forget_by_keyword():
    """Delete by keyword substring match"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("我喜欢Python"), _mem("密码是abc"), _mem("每周五例会")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = await mod.user_memory_forget_handler(keyword="密码")
        assert result["status"] == "success"
        assert result["current_memories"] == [_mem("我喜欢Python"), _mem("每周五例会")]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_forget_by_keyword")


async def test_user_memory_list():
    """List all memories"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("A"), _mem("B")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = await mod.user_memory_list_handler()
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["max_memory"] == 4
        assert result["max_task"] == 1
        assert result["memories"] == [_mem("A"), _mem("B")]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_list")


def test_truncate_over_limit():
    """Truncate permanent array > 5 on load"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem(f"记忆{i}") for i in range(8)]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod._read_memory_json()
        assert len(result["permanent"]) == mod.MAX_PERMANENT_ITEMS
        # Kept first 5
        assert result["permanent"][0] == _mem("记忆0")
        assert result["permanent"][4] == _mem("记忆4")

    mod._reset_memory_json_path()
    print("PASS: test_truncate_over_limit")


async def test_preserve_other_fields():
    """_write_permanent_only preserves identity/workspace/user fields"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a full memory.json with other fields
        full_data = {
            "identity": {"name": "妞妞", "personality": ["活泼"]},
            "workspace": {"path": "/tmp/test"},
            "user": {"name": "测试用户"},
            "permanent": [_mem("旧记忆")],
        }
        memory_path.write_text(json.dumps(full_data, ensure_ascii=False, indent=2), encoding="utf-8")

        _setup_module(memory_path)

        # Add a new memory
        result = await mod.user_memory_remember_handler(content="新记忆")
        assert result["status"] == "success"

        # Verify other fields are preserved
        saved = json.loads(memory_path.read_text(encoding="utf-8"))
        assert saved["identity"]["name"] == "妞妞"
        assert saved["workspace"]["path"] == "/tmp/test"
        assert saved["user"]["name"] == "测试用户"
        assert saved["permanent"] == [_mem("旧记忆"), _mem("新记忆")]

    mod._reset_memory_json_path()
    print("PASS: test_preserve_other_fields")


async def test_corrupted_file_rejection():
    """Corrupted memory.json should be rejected, not overwritten"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text("NOT VALID JSON{{{", encoding="utf-8")

        _setup_module(memory_path)

        result = await mod.user_memory_remember_handler(content="测试")
        assert result["status"] == "error"
        assert "损坏" in result["message"]

        # File should NOT be overwritten
        assert memory_path.read_text(encoding="utf-8") == "NOT VALID JSON{{{"

    mod._reset_memory_json_path()
    print("PASS: test_corrupted_file_rejection")


async def test_dedup_remember():
    """Reject case-insensitive duplicate content"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Exact duplicate
        result = await mod.user_memory_remember_handler(content="我喜欢Python")
        assert result["status"] == "error"
        assert "已存在" in result["message"]

        # Case-insensitive duplicate
        result = await mod.user_memory_remember_handler(content="我喜欢python")
        assert result["status"] == "error"
        assert "已存在" in result["message"]

        # Should still have only 1 item
        saved = json.loads(memory_path.read_text(encoding="utf-8"))
        assert len(saved["permanent"]) == 1

    mod._reset_memory_json_path()
    print("PASS: test_dedup_remember")


async def test_multi_keyword_match_warning():
    """Warn when multiple items match keyword"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("Python很好"), _mem("Python很强大"), _mem("Java也不错")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = await mod.user_memory_forget_handler(keyword="Python")
        assert result["status"] == "success"
        assert "还有1条" in result["message"]
        assert len(result["current_memories"]) == 2

    mod._reset_memory_json_path()
    print("PASS: test_multi_keyword_match_warning")


def test_normalize_permanent_migration():
    """_normalize_permanent converts old string format to new dict format"""
    old = ["我喜欢Python", "密码是abc"]
    normalized = mod._normalize_permanent(old)
    assert normalized == [_mem("我喜欢Python"), _mem("密码是abc")]

    # Mixed format (partial migration)
    mixed = ["旧字符串", _mem("新格式"), {"type": "task", "content": "工作便签"}]
    normalized = mod._normalize_permanent(mixed)
    assert normalized == [_mem("旧字符串"), _mem("新格式"), _mem("工作便签", "task")]

    # Already normalized
    already = [_mem("A"), _mem("B", "task")]
    assert mod._normalize_permanent(already) == already

    print("PASS: test_normalize_permanent_migration")


async def test_forget_task_clears_not_removes():
    """Forgetting a task item clears content instead of removing it"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("修复登录bug", "task"), _mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Forget by index (task is item 1)
        result = await mod.user_memory_forget_handler(index=1)
        assert result["status"] == "success"
        assert "清空" in result["message"]
        # Task slot still exists with empty content, memory item unchanged
        assert result["current_memories"] == [_mem("", "task"), _mem("我喜欢Python")]

    mod._reset_memory_json_path()
    print("PASS: test_forget_task_clears_not_removes")


async def test_forget_task_by_keyword_clears():
    """Forgetting a task by keyword clears content instead of removing"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("重构数据库", "task"), _mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = await mod.user_memory_forget_handler(keyword="重构")
        assert result["status"] == "success"
        assert "清空" in result["message"]
        assert result["current_memories"] == [_mem("", "task"), _mem("我喜欢Python")]

    mod._reset_memory_json_path()
    print("PASS: test_forget_task_by_keyword_clears")


if __name__ == "__main__":
    asyncio.run(test_user_memory_remember())
    asyncio.run(test_user_memory_remember_full())
    asyncio.run(test_task_type())
    asyncio.run(test_user_memory_forget_by_index())
    asyncio.run(test_user_memory_forget_by_keyword())
    asyncio.run(test_user_memory_list())
    test_truncate_over_limit()
    asyncio.run(test_preserve_other_fields())
    asyncio.run(test_corrupted_file_rejection())
    asyncio.run(test_dedup_remember())
    asyncio.run(test_multi_keyword_match_warning())
    test_normalize_permanent_migration()
    asyncio.run(test_forget_task_clears_not_removes())
    asyncio.run(test_forget_task_by_keyword_clears())
    print("\nAll tests passed!")

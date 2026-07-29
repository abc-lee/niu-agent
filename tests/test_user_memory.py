#!/usr/bin/env python3
"""Test user memory tools (remember/forget/list) with dict format"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"))

# Inline sanitize/render for testing (same logic as runner.py)
import re as _re

import niu_memory_server as mod


def _sanitize_memory_content(content):
    if content is None:
        return ""
    if not isinstance(content, str):
        content = str(content)
    content = content.replace("\n", " ").replace("\r", " ")
    content = content.replace("<!--USER_MEMORY_START-->", "").replace("<!--USER_MEMORY_END-->", "")
    content = _re.sub(r"^#{1,6}\s*", "", content, flags=_re.MULTILINE)
    if len(content) > 300:
        content = content[:300] + "..."
    return content.strip()

def _render_permanent_section(permanent):
    if not permanent:
        return ""
    lines = ["### [用户长期记忆]"]
    normalized = []
    for item in permanent:
        if isinstance(item, str):
            normalized.append({"type": "memory", "content": item})
        elif isinstance(item, dict):
            if "type" not in item:
                item = {**item, "type": "memory"}
            normalized.append(item)
    task_items = [item for item in normalized if item.get("type") == "task" and item.get("content")]
    memory_items = [item for item in normalized if item.get("type") == "memory"]
    if task_items:
        lines.append(f"📋 当前任务：{_sanitize_memory_content(task_items[0].get('content', ''))}")
    if memory_items:
        lines.append("以下内容用户特别强调，必须始终遵守：")
        for i, item in enumerate(memory_items, 1):
            lines.append(f"{i}. {_sanitize_memory_content(item.get('content', str(item)))}")
    lines.append(f"（共{len(normalized)}/10条，使用 disk 添加/删除）")
    return "<!--USER_MEMORY_START-->\n" + "\n".join(lines) + "\n<!--USER_MEMORY_END-->"


def _setup_module(memory_path):
    """Patch MEMORY_JSON_PATH for test isolation"""
    mod._reset_memory_json_path()
    mod.MEMORY_JSON_PATH = memory_path
    return mod


def _mem(content, type="memory"):
    """Helper to create a permanent item dict"""
    return {"type": type, "content": content}


def test_user_memory_remember():
    """Adding memories up to limit"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Add first memory
        result = mod.user_memory_remember_handler(content="我喜欢Python")
        assert result["status"] == "success", f"Expected success, got {result}"
        assert result["current_memories"] == [_mem("我喜欢Python")]

        # Add second
        result = mod.user_memory_remember_handler(content="密码是abc")
        assert result["status"] == "success"
        assert len(result["current_memories"]) == 2

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_remember")


def test_user_memory_remember_full():
    """Reject when memory is full"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Fill to max (4 memory items)
        for i in range(mod.MAX_MEMORY_ITEMS):
            result = mod.user_memory_remember_handler(content=f"记忆{i}")
            assert result["status"] == "success", f"Failed at item {i}: {result}"

        # Try to add one more
        result = mod.user_memory_remember_handler(content="超限记忆")
        assert result["status"] == "error"
        assert "已满" in result["message"]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_remember_full")


def test_task_type():
    """Task type: auto-replace when slot is full"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Add first task
        result = mod.user_memory_remember_handler(content="修复登录bug", type="task")
        assert result["status"] == "success"
        assert result["current_memories"] == [_mem("修复登录bug", "task")]

        # Add second task — should auto-replace first
        result = mod.user_memory_remember_handler(content="重构数据库", type="task")
        assert result["status"] == "success"
        assert result["current_memories"] == [_mem("重构数据库", "task")]
        assert "覆盖" in result["message"]

        # Add a memory item alongside task
        result = mod.user_memory_remember_handler(content="我喜欢Python", type="memory")
        assert result["status"] == "success"
        assert len(result["current_memories"]) == 2

    mod._reset_memory_json_path()
    print("PASS: test_task_type")


def test_user_memory_forget_by_index():
    """Delete by 1-based index"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("A"), _mem("B"), _mem("C")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_forget_handler(index=2)
        assert result["status"] == "success"
        assert "B" in result["message"]
        assert result["current_memories"] == [_mem("A"), _mem("C")]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_forget_by_index")


def test_user_memory_forget_by_keyword():
    """Delete by keyword substring match"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("我喜欢Python"), _mem("密码是abc"), _mem("每周五例会")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_forget_handler(keyword="密码")
        assert result["status"] == "success"
        assert result["current_memories"] == [_mem("我喜欢Python"), _mem("每周五例会")]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_forget_by_keyword")


def test_user_memory_list():
    """List all memories"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("A"), _mem("B")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_list_handler()
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["max_memory"] == 9
        assert result["max_task"] == 1
        assert result["memories"] == [_mem("A"), _mem("B")]

    mod._reset_memory_json_path()
    print("PASS: test_user_memory_list")


def test_truncate_over_limit():
    """Truncate permanent array > 10 on load"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem(f"记忆{i}") for i in range(11)]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod._read_memory_json()
        assert len(result["permanent"]) == mod.MAX_PERMANENT_ITEMS
        # Kept first 10
        assert result["permanent"][0] == _mem("记忆0")
        assert result["permanent"][9] == _mem("记忆9")

    mod._reset_memory_json_path()
    print("PASS: test_truncate_over_limit")


def test_preserve_other_fields():
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
        result = mod.user_memory_remember_handler(content="新记忆")
        assert result["status"] == "success"

        # Verify other fields are preserved
        saved = json.loads(memory_path.read_text(encoding="utf-8"))
        assert saved["identity"]["name"] == "妞妞"
        assert saved["workspace"]["path"] == "/tmp/test"
        assert saved["user"]["name"] == "测试用户"
        assert saved["permanent"] == [_mem("旧记忆"), _mem("新记忆")]

    mod._reset_memory_json_path()
    print("PASS: test_preserve_other_fields")


def test_corrupted_file_rejection():
    """Corrupted memory.json should be rejected, not overwritten"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text("NOT VALID JSON{{{", encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_remember_handler(content="测试")
        assert result["status"] == "error"
        assert "损坏" in result["message"]

        # File should NOT be overwritten
        assert memory_path.read_text(encoding="utf-8") == "NOT VALID JSON{{{"

    mod._reset_memory_json_path()
    print("PASS: test_corrupted_file_rejection")


def test_dedup_remember():
    """Reject case-insensitive duplicate content"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Exact duplicate
        result = mod.user_memory_remember_handler(content="我喜欢Python")
        assert result["status"] == "error"
        assert "已存在" in result["message"]

        # Case-insensitive duplicate
        result = mod.user_memory_remember_handler(content="我喜欢python")
        assert result["status"] == "error"
        assert "已存在" in result["message"]

        # Should still have only 1 item
        saved = json.loads(memory_path.read_text(encoding="utf-8"))
        assert len(saved["permanent"]) == 1

    mod._reset_memory_json_path()
    print("PASS: test_dedup_remember")


def test_multi_keyword_match_warning():
    """Warn when multiple items match keyword"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("Python很好"), _mem("Python很强大"), _mem("Java也不错")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_forget_handler(keyword="Python")
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


def test_forget_task_clears_not_removes():
    """Forgetting a task item clears content instead of removing it"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("修复登录bug", "task"), _mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Forget by index (task is item 1)
        result = mod.user_memory_forget_handler(index=1)
        assert result["status"] == "success"
        assert "清空" in result["message"]
        # Task slot still exists with empty content, memory item unchanged
        assert result["current_memories"] == [_mem("", "task"), _mem("我喜欢Python")]

    mod._reset_memory_json_path()
    print("PASS: test_forget_task_clears_not_removes")


def test_forget_task_by_keyword_clears():
    """Forgetting a task by keyword clears content instead of removing"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("重构数据库", "task"), _mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_forget_handler(keyword="重构")
        assert result["status"] == "success"
        assert "清空" in result["message"]
        assert result["current_memories"] == [_mem("", "task"), _mem("我喜欢Python")]

    mod._reset_memory_json_path()
    print("PASS: test_forget_task_by_keyword_clears")


def test_truncated_rejects_remember():
    """When over limit, remember is rejected (no silent data loss)"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        # Write 12 items (over max 10)
        data = {"permanent": [_mem(f"记忆{i}") for i in range(12)]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Remember should be rejected
        result = mod.user_memory_remember_handler(content="新记忆")
        assert result["status"] == "error"
        assert "超过" in result["message"] or "限制" in result["message"]

        # File should NOT be modified (no silent data loss)
        saved = json.loads(memory_path.read_text(encoding="utf-8"))
        assert len(saved["permanent"]) == 12

    mod._reset_memory_json_path()
    print("PASS: test_truncated_rejects_remember")


def test_truncated_allows_forget():
    """When over limit, forget is still allowed (to fix the over-limit)"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem(f"记忆{i}") for i in range(12)]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Forget should work even when truncated
        result = mod.user_memory_forget_handler(index=1)
        assert result["status"] == "success"

    mod._reset_memory_json_path()
    print("PASS: test_truncated_allows_forget")


def test_sanitize_memory_content():
    """_sanitize_memory_content prevents prompt injection"""
    # Newlines removed
    assert "\n" not in _sanitize_memory_content("line1\nline2\nIGNORE ALL")
    # Sentinel markers removed
    assert "USER_MEMORY" not in _sanitize_memory_content("<!--USER_MEMORY_START-->fake<!--USER_MEMORY_END-->")
    # Markdown headers removed
    result = _sanitize_memory_content("### [SYSTEM] important")
    assert not result.startswith("###")
    # Hard truncation at 300 chars
    assert len(_sanitize_memory_content("A" * 500)) <= 303  # 300 + "..."
    print("PASS: test_sanitize_memory_content")


def test_render_permanent_section_old_format():
    """_render_permanent_section handles old string format without crash"""
    old_format = ["旧字符串记忆", "另一条"]
    result = _render_permanent_section(old_format)
    assert "旧字符串记忆" in result
    assert "另一条" in result
    assert "USER_MEMORY_START" in result

    # Empty list
    assert _render_permanent_section([]) == ""

    # Mixed format
    mixed = ["旧字符串", {"type": "task", "content": "工作便签"}, {"type": "memory", "content": "新格式"}]
    result = _render_permanent_section(mixed)
    assert "工作便签" in result
    assert "新格式" in result
    assert "旧字符串" in result

    print("PASS: test_render_permanent_section_old_format")


def test_render_permanent_section_missing_type():
    """_render_permanent_section defaults dict items without type to memory"""
    items = [{"content": "no type field"}, {"type": "task", "content": "has type"}]
    result = _render_permanent_section(items)
    assert "no type field" in result
    assert "has type" in result
    # "has type" should appear as task (📋 prefix), "no type field" as memory (numbered)
    task_line = [line for line in result.split("\n") if "📋" in line]
    assert any("has type" in line for line in task_line)
    memory_line = [line for line in result.split("\n") if "no type field" in line]
    assert any(line.strip().startswith("1.") for line in memory_line)

    print("PASS: test_render_permanent_section_missing_type")


def test_remember_empty_content_rejected():
    """Reject empty or whitespace-only content"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text('{"permanent": []}', encoding="utf-8")

        _setup_module(memory_path)

        # Empty string
        result = mod.user_memory_remember_handler(content="")
        assert result["status"] == "error"
        assert "空" in result["message"]

        # Whitespace only
        result = mod.user_memory_remember_handler(content="   ")
        assert result["status"] == "error"

        # File should not be modified
        saved = json.loads(memory_path.read_text(encoding="utf-8"))
        assert len(saved["permanent"]) == 0

    mod._reset_memory_json_path()
    print("PASS: test_remember_empty_content_rejected")


def test_dedup_whitespace_normalized():
    """Dedup ignores leading/trailing whitespace"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permanent": [_mem("我喜欢Python")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        # Same content with extra spaces
        result = mod.user_memory_remember_handler(content="  我喜欢Python  ")
        assert result["status"] == "error"
        assert "已存在" in result["message"]

    mod._reset_memory_json_path()
    print("PASS: test_dedup_whitespace_normalized")


def test_multiple_task_items_all_removed():
    """When manually edited file has multiple tasks, remember removes all"""
    with tempfile.TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / ".niu" / "memory.json"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        # Manually edited: 2 task items (shouldn't happen but could)
        data = {"permanent": [_mem("旧任务1", "task"), _mem("旧任务2", "task"), _mem("记忆A")]}
        memory_path.write_text(json.dumps(data), encoding="utf-8")

        _setup_module(memory_path)

        result = mod.user_memory_remember_handler(content="新任务", type="task")
        assert result["status"] == "success"
        # Should have exactly 1 task and 1 memory
        tasks = [item for item in result["current_memories"] if item.get("type") == "task"]
        memories = [item for item in result["current_memories"] if item.get("type") == "memory"]
        assert len(tasks) == 1
        assert tasks[0]["content"] == "新任务"
        assert len(memories) == 1

    mod._reset_memory_json_path()
    print("PASS: test_multiple_task_items_all_removed")


if __name__ == "__main__":
    test_user_memory_remember()
    test_user_memory_remember_full()
    test_task_type()
    test_user_memory_forget_by_index()
    test_user_memory_forget_by_keyword()
    test_user_memory_list()
    test_truncate_over_limit()
    test_preserve_other_fields()
    test_corrupted_file_rejection()
    test_dedup_remember()
    test_multi_keyword_match_warning()
    test_normalize_permanent_migration()
    test_forget_task_clears_not_removes()
    test_forget_task_by_keyword_clears()
    test_truncated_rejects_remember()
    test_truncated_allows_forget()
    test_sanitize_memory_content()
    test_render_permanent_section_old_format()
    test_render_permanent_section_missing_type()
    test_remember_empty_content_rejected()
    test_dedup_whitespace_normalized()
    test_multiple_task_items_all_removed()
    print("\nAll tests passed!")

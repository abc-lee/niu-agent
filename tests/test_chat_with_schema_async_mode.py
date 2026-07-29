"""验证 allowAsync=true 的子 Agent schema 含 async_mode 参数；allowAsync=false 的不含。"""
import os
import sys

# 兜底：将项目根目录加入 sys.path，避免 conftest 路径差异问题
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest
import yaml

from agent.runner import get_tools_schema


def test_schema_includes_async_mode_for_allow_async_subagent():
    """file-processor（allowAsync=true）的 chat-with-file-processor schema 含 async_mode。"""
    config_path = os.path.join(
        PROJECT_ROOT,
        "config",
        "agents",
        "file-processor.md",
    )

    with open(config_path, encoding="utf-8") as f:
        content = f.read()

    parts = content.split("---", 2)
    assert len(parts) >= 3, "file-processor.md frontmatter 解析失败"
    fm = yaml.safe_load(parts[1])

    if not fm.get("allowAsync", False):
        pytest.skip("file-processor.md 还没设 allowAsync=true（Task 10 后续步骤会设）")

    tools = get_tools_schema()
    chat_with_fp = next(
        (t for t in tools if t.get("function", {}).get("name") == "chat-with-file-processor"),
        None,
    )
    assert chat_with_fp is not None, "chat-with-file-processor schema 未找到"

    props = chat_with_fp["function"]["parameters"].get("properties", {})
    assert "async_mode" in props, "async_mode 应该在 allowAsync=true 的子 Agent schema 里"
    assert props["async_mode"].get("type") == "boolean"
    assert props["async_mode"].get("default", False) is False


def test_schema_excludes_async_mode_for_sync_only_subagent():
    """event-manager（allowAsync 未设，默认 false）的 schema 不含 async_mode。"""
    tools = get_tools_schema()
    chat_with_em = next(
        (t for t in tools if t.get("function", {}).get("name") == "chat-with-event-manager"),
        None,
    )
    assert chat_with_em is not None, "chat-with-event-manager schema 未找到"

    props = chat_with_em["function"]["parameters"].get("properties", {})
    assert "async_mode" not in props, "async_mode 不应该出现在 allowAsync=false 的子 Agent schema 里"

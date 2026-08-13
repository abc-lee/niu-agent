"""压缩后悬空清理测试：空壳 assistant 删除 + 原始形态（工具调用锚点）保留 + 孤立 tool 清理。"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# 确保 niu_api 可 import（既有惯例：单层 parent，参照 test_compress_history.py）
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _cleanup_orphan_tool_messages  # noqa: E402


def _msg(mid, role, content="", tool_calls=None, tool_call_id=""):
    """tool_calls 用 list 形态（生产路径：store.get_messages 经 _safe_json 返回已解析 list[dict]，
    不是 JSON 字符串——测试必须镜像生产形态，防字符串分支假通过）。"""
    m = MagicMock()
    m.id = mid
    m.role = role
    m.content = content
    m.tool_calls = tool_calls
    m.tool_call_id = tool_call_id
    return m


def _run(store):
    return asyncio.run(_cleanup_orphan_tool_messages(store))


def _make_store(messages):
    store = MagicMock()
    store.db_path = ":memory:"
    store.get_messages = AsyncMock(return_value=messages)
    store.delete_messages_by_ids = AsyncMock(return_value=True)
    return store


def test_cleanup_deletes_empty_shell_assistant():
    """空壳 assistant（content 空 + tool_calls 空 + tool_call_id 空）→ 删除。"""
    shell = _msg("shell-1", "assistant", content="", tool_calls=[], tool_call_id="")
    normal = _msg("normal-1", "assistant", content="你好", tool_calls=[])
    store = _make_store([shell, normal])
    _run(store)
    store.delete_messages_by_ids.assert_awaited_once_with(["shell-1"])


def test_cleanup_keeps_tool_call_anchor():
    """原始形态（content 空但 tool_calls 非空）→ 保留（工具调用锚点，不删不乱）。"""
    anchor = _msg("anchor-1", "assistant", content="",
                  tool_calls=[{"id": "call_abc", "type": "function",
                               "function": {"name": "read", "arguments": "{}"}}])
    store = _make_store([anchor])
    _run(store)
    store.delete_messages_by_ids.assert_not_awaited()


def test_cleanup_keeps_normal_assistant_and_deletes_orphan_tool():
    """正常 assistant 保留；孤立 tool（tool_call_id 无归属）删除（原有逻辑保持）。"""
    normal = _msg("normal-1", "assistant", content="你好", tool_calls=[])
    orphan_tool = _msg("orphan-1", "tool", content="[工具结果] x", tool_call_id="call_dead")
    store = _make_store([normal, orphan_tool])
    _run(store)
    store.delete_messages_by_ids.assert_awaited_once_with(["orphan-1"])


def test_cleanup_keeps_tool_with_valid_owner():
    """有归属的 tool（tool_call_id 存在于某 assistant 的 tool_calls）→ 保留。"""
    owner = _msg("owner-1", "assistant", content="调工具",
                 tool_calls=[{"id": "call_live", "type": "function",
                              "function": {"name": "read", "arguments": "{}"}}])
    valid_tool = _msg("tool-1", "tool", content="[工具结果] ok", tool_call_id="call_live")
    store = _make_store([owner, valid_tool])
    _run(store)
    store.delete_messages_by_ids.assert_not_awaited()


def test_cleanup_mixed_scenario():
    """混合场景：空壳删、原始形态留、孤立 tool 删、正常留。"""
    shell = _msg("shell-1", "assistant", content="", tool_calls=[], tool_call_id="")
    anchor = _msg("anchor-1", "assistant", content="",
                  tool_calls=[{"id": "call_a", "type": "function",
                               "function": {"name": "read", "arguments": "{}"}}])
    normal = _msg("normal-1", "assistant", content="正常回复", tool_calls=[])
    orphan_tool = _msg("orphan-1", "tool", content="[工具结果] 悬空", tool_call_id="call_dead")
    valid_tool = _msg("tool-1", "tool", content="[工具结果] 有效", tool_call_id="call_a")
    store = _make_store([shell, anchor, normal, orphan_tool, valid_tool])
    _run(store)
    # 空壳 + 孤立 tool 被删；anchor/normal/valid_tool 保留
    deleted = store.delete_messages_by_ids.await_args.args[0]
    assert set(deleted) == {"shell-1", "orphan-1"}

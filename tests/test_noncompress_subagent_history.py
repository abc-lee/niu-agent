"""journal 增量导出通道单元测试（T7：history 注入 → 文件自读）。

背景（T7）：journal-agent 输入通道改为自读导出 md——程序把 DB 增量导出到工作集
文件（[N] 编号），子 Agent 自读后回报 processed_up_to=N，程序按 idx_to_id 映射
推进游标。原 _build_plain_history/_build_incremental_msg_text 随 history 注入通道
退役整删。

本测试验证：
1. _export_journal_increment 游标下界（增量区间、找不到游标降级全量、零增量）
2. 导出格式：[N] 头行编号 + role/created_at，idx_to_id 映射与编号一致
3. tool_calls / tool_call_id 标注与 role=tool 超长正文截断（md_mirror 先例）
4. _parse_processed_up_to 解析各种格式（= / : / 空格，大小写不敏感）
5. _parse_processed_up_to 未找到返回 None
"""
import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _export_journal_increment, _parse_processed_up_to  # noqa: E402


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""

    def __init__(self, mid, role="user", content="hello", tool_calls=None, tool_call_id="", created_at="t"):
        self.id = mid
        self.role = role
        self.content = content
        self.tool_calls = tool_calls if tool_calls is not None else []
        self.tool_call_id = tool_call_id
        self.created_at = created_at


def test_export_increment_cursor_lower_bound(tmp_path):
    """游标之后的消息导出；编号 1-based 与 idx_to_id 一致。"""
    messages = [
        FakeMsg("uuid-0", content="旧消息"),
        FakeMsg("uuid-1", content="你好"),
        FakeMsg("uuid-2", content="做完了 A"),
        FakeMsg("uuid-3", content="收到"),
    ]
    out = tmp_path / "workset.md"
    msg_ids, idx_to_id = _export_journal_increment(messages, "uuid-0", out)

    assert msg_ids == ["uuid-1", "uuid-2", "uuid-3"]
    assert idx_to_id == {1: "uuid-1", 2: "uuid-2", 3: "uuid-3"}
    text = out.read_text(encoding="utf-8")
    assert "[1] user" in text and "[3] user" in text
    assert "旧消息" not in text, "游标之前的消息不得出现在工作集"
    assert "你好" in text


def test_export_increment_missing_cursor_degrades_full(tmp_path):
    """游标 UUID 找不到 → 降级全量处理。"""
    messages = [FakeMsg("m1"), FakeMsg("m2")]
    out = tmp_path / "workset.md"
    msg_ids, idx_to_id = _export_journal_increment(messages, "not-exist", out)
    assert msg_ids == ["m1", "m2"]
    assert idx_to_id == {1: "m1", 2: "m2"}


def test_export_increment_empty_when_no_new_messages(tmp_path):
    """游标指向末条 → 零增量，返回空且不写文件。"""
    messages = [FakeMsg("m1")]
    out = tmp_path / "workset.md"
    msg_ids, idx_to_id = _export_journal_increment(messages, "m1", out)
    assert msg_ids == [] and idx_to_id == {}
    assert not out.exists()


def test_export_increment_tool_annotations_and_truncation(tmp_path):
    """tool_calls/tool_call_id 进头行标注；role=tool 超长正文截断。"""
    from agent.md_mirror import TOOL_OUTPUT_MARKER

    long_output = "x" * 5000
    messages = [
        FakeMsg("a1", role="assistant", content="", tool_calls=[{"function": {"name": "search"}}]),
        FakeMsg("t1", role="tool", content=long_output, tool_call_id="call-9"),
    ]
    out = tmp_path / "workset.md"
    _export_journal_increment(messages, "", out)

    text = out.read_text(encoding="utf-8")
    assert "[1] assistant" in text and "(tool_calls: search)" in text
    assert "[2] tool" in text and "(answers tool_call_id=call-9)" in text
    assert TOOL_OUTPUT_MARKER in text, "超长工具输出应按 md_mirror 先例截断"


def test_parse_processed_up_to_various_formats():
    """_parse_processed_up_to 支持 = / : / 空格分隔，大小写不敏感。"""
    assert _parse_processed_up_to("处理完成\nprocessed_up_to=15") == 15
    assert _parse_processed_up_to("processed_up_to: 15") == 15
    assert _parse_processed_up_to("PROCESSED_UP_TO=15") == 15
    assert _parse_processed_up_to("processed_up_to 15") == 15
    assert _parse_processed_up_to("processed_up_to=3\nprocessed_up_to=15") == 3


def test_parse_processed_up_to_not_found_returns_none():
    """未找到 processed_up_to= 时返回 None。"""
    assert _parse_processed_up_to("处理完成，无标记") is None
    assert _parse_processed_up_to("") is None
    assert _parse_processed_up_to("processed_up_to=") is None
    assert _parse_processed_up_to("processed_up_to=abc") is None  # 非整数

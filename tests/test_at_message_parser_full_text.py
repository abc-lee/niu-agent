"""T3 主→子 @ 整段传递测试（at_message_parser 公共前言 + @ 后内容）。

方案 v2.5：
- content = 公共前言 + @ 后内容——公共前言 = reply_text[:第一个 @ 匹配.start()]（strip 后参与拼接防双换行）
- 多 @：每个 @ 消息都带公共前言——空 @ 内容仍过滤
- strip_at_messages / format_for_db 不变
"""
from agent.at_message_parser import extract_at_messages, format_for_db, strip_at_messages


def test_extract_preface_strip_normalized():
    """公共前言 strip 归一化：前言以换行结尾时拼接不产双换行（单换行分隔）。"""
    reply = "好的，我处理。\n\n@file-processor-a1b2 是的，用 OCR 处理"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    # 前言 "好的，我处理。\n\n".strip() → "好的，我处理。"——拼接用单 \n
    assert msgs[0]["content"] == "好的，我处理。\n是的，用 OCR 处理"


def test_extract_single_at_full_segment():
    """单 @ 场景 = 完整整段（前言 + @ 后内容）。"""
    reply = "我确认一下。@file-processor-a1b2 用 OCR 继续处理"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[0]["content"] == "我确认一下。\n用 OCR 继续处理"


def test_extract_no_preface_keeps_old_behavior():
    """@ 在行首（无公共前言）→ content 保持 @ 后内容（既有行为不回归）。"""
    reply = "@file-processor-a1b2 是的，用 OCR 处理"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "是的，用 OCR 处理"


def test_extract_multiple_at_each_with_preface():
    """多 @ 场景：每个 @ 消息都带公共前言（多 @ 不互相污染）。"""
    reply = "收到。\n@file-processor-a1b2 处理 A\n@context-manager-c3d4 处理 B"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 2
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[0]["content"] == "收到。\n处理 A"
    assert msgs[1]["target"] == "context-manager-c3d4"
    assert msgs[1]["content"] == "收到。\n处理 B"


def test_extract_empty_at_content_still_filtered():
    """空 @ 内容仍过滤（既有行为保留——相邻 @ 目标空 content）。"""
    reply = "前言说明 @file-processor-a1b2 @context-manager-c3d4 处理 B"
    msgs = extract_at_messages(reply)
    # 第一个 @ 后内容为空（紧接着下一个 @）→ 过滤；第二个正常提取并带前言
    assert len(msgs) == 1
    assert msgs[0]["target"] == "context-manager-c3d4"
    assert msgs[0]["content"] == "前言说明\n处理 B"


def test_extract_no_at_returns_empty():
    """无 @ 消息 → 空列表（不回归）。"""
    assert extract_at_messages("好的，我处理这个文档。") == []


def test_strip_at_messages_unchanged():
    """strip_at_messages 不变：@ 段剥离给用户，前言保留给用户（用户看到自己说的整段）。"""
    reply = "好的。\n@file-processor-a1b2 是的，用 OCR"
    stripped = strip_at_messages(reply)
    assert "@file-processor" not in stripped
    assert "好的" in stripped


def test_format_for_db_unchanged():
    """format_for_db 不变：@目标 [发送者] 内容。"""
    msg = {"target": "file-processor-a1b2", "content": "好的，我处理。\n用 OCR", "sender": "主Agent"}
    assert format_for_db(msg) == "@file-processor-a1b2 [主Agent] 好的，我处理。\n用 OCR"

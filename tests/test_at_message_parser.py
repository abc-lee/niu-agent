"""验证 @ 消息解析器。"""
from agent.at_message_parser import extract_at_messages, format_for_db, strip_at_messages


def test_extract_single_at_message():
    """单条 @ 消息提取。"""
    reply = "好的，我处理。\n@file-processor-a1b2 是的，用 OCR 处理"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[0]["content"] == "是的，用 OCR 处理"
    assert msgs[0]["sender"] == "主Agent"


def test_extract_multiple_at_messages():
    """多条 @ 消息提取（含多连字符类型）。"""
    reply = "@file-processor-a1b2 是的，用 OCR\n@context-manager-c3d4 暂时不用压缩"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 2
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[1]["target"] == "context-manager-c3d4"  # 多连字符类型


def test_extract_multi_hyphen_type():
    """多连字符类型子 Agent 名（如 context-manager、brain-region）能正确匹配。"""
    reply = "@context-manager-c3d4 压缩吧\n@brain-region-d5e6 激活"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 2
    assert msgs[0]["target"] == "context-manager-c3d4"
    assert msgs[1]["target"] == "brain-region-d5e6"


def test_extract_no_at_message():
    """无 @ 消息时返回空列表。"""
    reply = "好的，我处理这个文档。"
    msgs = extract_at_messages(reply)
    assert msgs == []


def test_extract_stop_command():
    """/stop 指令提取。"""
    reply = "@file-processor-a1b2 /stop"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "/stop"


def test_strip_at_messages():
    """从回复文本移除 @ 消息。"""
    reply = "好的。\n@file-processor-a1b2 是的，用 OCR"
    stripped = strip_at_messages(reply)
    assert "@file-processor" not in stripped
    assert "好的" in stripped


def test_format_for_db():
    """提取后格式化为 db 存储格式。"""
    msg = {"target": "file-processor-a1b2", "content": "用 OCR", "sender": "主Agent"}
    formatted = format_for_db(msg)
    assert formatted == "@file-processor-a1b2 [主Agent] 用 OCR"


def test_extract_non_hex_suffix_rejected():
    """非 hex 后缀（如 c3g4）不被匹配。"""
    reply = "@file-processor-c3g4 测试"
    msgs = extract_at_messages(reply)
    assert msgs == []


def test_persist_agent_reply_strips_assistant_content_in_rv_path():
    """rv 路径下 assistant content 也 strip @ 消息，避免重复入库。"""
    import inspect

    from niu_api import chat
    source = inspect.getsource(chat.persist_agent_reply)
    # persist_agent_reply 应在 rv 路径遍历也调 strip_at_messages（≥2 次：
    # 一次 strip full_reply，一次 rv 遍历 strip assistant content）
    assert source.count("strip_at_messages") >= 2, (
        "persist_agent_reply 应在 rv 路径也调 strip_at_messages"
    )
    # rv 路径遍历应含 strip 后为空跳过的保护
    assert "not content.strip()" in source, (
        "rv 路径 strip 后为空应跳过（@ 消息已单独存 subagent_msg）"
    )

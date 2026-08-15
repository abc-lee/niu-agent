"""验证 @ 消息解析器。"""
from agent.at_message_parser import extract_at_messages, format_for_db, strip_at_messages


def test_extract_single_at_message():
    """单条 @ 消息提取（T3：content = 公共前言 + @ 后内容——主→子整段传递）。"""
    reply = "好的，我处理。\n@file-processor-a1b2 是的，用 OCR 处理"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[0]["content"] == "好的，我处理。\n是的，用 OCR 处理"
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


def test_extract_non_hex_suffix_now_extracted_as_kebab():
    """非 hex 后缀（如 c3g4）按 kebab 名兼容提取（hex 后缀从"必须 4 位合法"放宽为"可选"）。"""
    reply = "@file-processor-c3g4 测试"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["target"] == "file-processor-c3g4"  # 整体作为 kebab 名（含数字段）


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


def test_strip_preserves_blank_lines():
    """LLM 输出块间空行必须保留（飞书 CardKit 块闭合依赖空行）——2026-08-15 实证：日志\n\n您看 被删成单 \n。"""
    reply = "11. **日志管理** — 调用journal-agent记录今日工作日志\n\n您看有需要修改或补充的吗？"
    stripped = strip_at_messages(reply)
    assert stripped == reply  # 无 @ 消息时输出与输入逐字节一致（含空行）


def test_strip_removes_at_keeps_blank_lines():
    """@ 消息剥离 + @ 前置文本空行保留（@ 在末尾——单 @ 尾随文本被吞为文档化行为 test_at_sync_name.py L28-36，不在本测试范围）。"""
    reply = "A\n\nC\n\n@file-processor-a1b2 处理 B"
    stripped = strip_at_messages(reply)
    assert stripped == "A\n\nC"  # @ 段剥离（含内容）→ 前置文本 + 空行结构保留

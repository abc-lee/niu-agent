"""runner 流式 notify_stream 闸门必须统一调 should_push_im() 单一判定入口——
定时任务（force-only）回合 IM 端也有流式卡片，与 chat_session 推送闸门（compat.py）同口径。
结构断言模式与 TestChatQueueForceWiring / test_chat_session_im_push 一致。"""
from pathlib import Path


def test_stream_reply_chunk_gate_uses_unified_gate():
    src = Path("agent/runner.py").read_text(encoding="utf-8")
    # reply chunk 流式闸门（chunk.content 非空守卫 + 单一入口）
    assert "chunk.content and self.should_push_im()" in src, \
        "reply chunk 流式闸门必须调 should_push_im()"


def test_stream_str_chunk_gate_uses_unified_gate():
    src = Path("agent/runner.py").read_text(encoding="utf-8")
    # 普通 str chunk 流式闸门（stream_error 文本进 accumulated 路径）
    assert "chunk and self.should_push_im()" in src, \
        "str chunk 流式闸门必须调 should_push_im()"


def test_stream_final_gate_uses_unified_gate():
    src = Path("agent/runner.py").read_text(encoding="utf-8")
    # finally is_final 流式结束闸门
    assert "is_connected and self.should_push_im()" in src, \
        "is_final 流式闸门必须调 should_push_im()"


def test_stream_gate_count_exactly_three():
    src = Path("agent/runner.py").read_text(encoding="utf-8")
    # 流式闸门恰好三处调用（reply chunk / str chunk / finally is_final）——防漏改或误加。
    # 注意 should_push_im 的方法定义（def should_push_im）不含 "self.should_push_im()" 子串，不计入。
    assert src.count("self.should_push_im()") == 3, \
        f"流式闸门应恰好 3 处调用，实际 {src.count('self.should_push_im()')}"

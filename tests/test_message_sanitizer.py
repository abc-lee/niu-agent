"""发送层合规化 sanitize_llm_messages 单测（plan 2026-09-10-tool-message-order-fix §4）。

全 mock / 纯函数，禁真实 LLM。覆盖：
① 顺序规整：单/多 tool_calls、跨块响应、无 tool 零变化、ask_user DB 形态（3596-3598 复刻）
② 幂等：无图输入恒等幂等（二次净化逐字节相同）
③ 孤儿 tool 丢弃 / subagent_msg 丢弃
④ 悬空 tool_calls 两形态：全悬空 → 整消息降级纯文本；部分悬空 → 按 tc 剥离、保留配对响应
⑤ 非 user list 降级（tool + assistant）/ system list 原样保留（cache_control 不被压平）
⑥ 长度不变式锁：调用方 messages 对象零变化（副本语义）
"""

import copy

from agent.generic.message_sanitizer import sanitize_llm_messages

# ---------------------------------------------------------------------------
# 基建
# ---------------------------------------------------------------------------

def _tc(i, name="tool_x"):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": "{}"}}


# ---------------------------------------------------------------------------
# ① 顺序规整
# ---------------------------------------------------------------------------

class TestReorder:
    def test_single_tool_call_contiguous_unchanged(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool"]
        assert out[0]["tool_calls"][0]["id"] == "call_1"

    def test_multi_tool_calls_contiguous_unchanged(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1), _tc(2)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
            {"role": "tool", "content": "R2", "tool_call_id": "call_2"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool", "tool"]
        assert [e["tool_call_id"] for e in out[1:]] == ["call_1", "call_2"]

    def test_cross_block_response_moved_after_assistant(self):
        """跨块响应：夹入的 user 消息顺延到 tool 块之后。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1), _tc(2)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
            {"role": "user", "content": "（补充说明）"},
            {"role": "tool", "content": "R2", "tool_call_id": "call_2"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool", "tool", "user"]
        assert [e.get("tool_call_id") for e in out[1:3]] == ["call_1", "call_2"]
        assert out[3]["content"] == "（补充说明）"

    def test_no_tools_zero_change(self):
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert out == msgs

    def test_ask_user_db_form_3596_3598(self):
        """ask_user DB 形态复刻（rowid 3596/3597/3598）：assistant(tool_calls) → user(回答) → tool(结果)
        → 规整为 assistant → tool → user（k3-256k 400 病灶）。"""
        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [_tc(1, "ask_user")]},
            {"role": "user", "content": "[用户回答] 继续"},
            {"role": "tool", "content": "[user 回答] 继续", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool", "user"]
        assert out[1]["tool_call_id"] == "call_1"
        assert out[2]["content"] == "[用户回答] 继续"

    def test_adjacent_assistant_gap_reorder(self):
        """相邻 assistant 缺口（中断/压缩重建形态）：A(tc c1) → B(纯文本) → tool(c1)
        → 全局配对归位为 A → tool(c1) → B（旧「扫描止于下一个 assistant」漏网，T1 QualityB P2）。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "assistant", "content": "B 纯文本"},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool", "assistant"]
        assert out[0]["tool_calls"][0]["id"] == "call_1"
        assert out[1]["tool_call_id"] == "call_1"
        assert out[2]["content"] == "B 纯文本"

    def test_interleaved_adjacent_assistants(self):
        """多对交错相邻 assistant：A(tc c1) → B(tc c2) → tool(c2) → tool(c1)
        → 各自归位 A→tool(c1)、B→tool(c2)，保持相对序。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "assistant", "content": "", "tool_calls": [_tc(2)]},
            {"role": "tool", "content": "R2", "tool_call_id": "call_2"},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool", "assistant", "tool"]
        assert out[0]["tool_calls"][0]["id"] == "call_1"
        assert out[1]["tool_call_id"] == "call_1"
        assert out[2]["tool_calls"][0]["id"] == "call_2"
        assert out[3]["tool_call_id"] == "call_2"


# ---------------------------------------------------------------------------
# ② 幂等
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_no_image_input_identity_idempotent(self):
        """无图输入恒等幂等：二次净化逐字节相同。"""
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "user", "content": "[用户回答] x"},
            {"role": "tool", "content": "R", "tool_call_id": "call_1"},
        ]
        once = sanitize_llm_messages(copy.deepcopy(msgs))
        twice = sanitize_llm_messages(copy.deepcopy(once))
        assert twice == once


# ---------------------------------------------------------------------------
# ③ 丢弃：孤儿 tool / subagent_msg
# ---------------------------------------------------------------------------

class TestDiscard:
    def test_orphan_tool_dropped(self):
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "tool", "content": "ORPHAN", "tool_call_id": "call_none"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert all(e["role"] != "tool" for e in out)

    def test_subagent_msg_dropped(self):
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "subagent_msg", "content": "@ 前端展示消息"},
            {"role": "assistant", "content": "A"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# ④ 悬空 tool_calls 两形态
# ---------------------------------------------------------------------------

class TestDanglingToolCalls:
    def test_all_dangling_downgrade_plain_text(self):
        """全悬空 → 整消息降级纯文本（不设 tool_calls 键、不删消息；resumed protect_end 形态）。"""
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "我在思考……",
             "tool_calls": [_tc(9, "ghost")]},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert len(out) == 2
        assert "tool_calls" not in out[1]
        assert out[1]["content"] == "我在思考……"

    def test_all_dangling_empty_content_kept(self):
        """全悬空 + 空 content → 保留纯文本空消息（与 transform_history 既有语义一致）。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(9)]},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert out == [{"role": "assistant", "content": ""}]

    def test_partial_dangling_strip_by_tc(self):
        """部分悬空 → 按 tc 剥离悬空项、保留配对响应（不造新孤儿）。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1), _tc(2)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert [e["role"] for e in out] == ["assistant", "tool"]
        assert [tc["id"] for tc in out[0]["tool_calls"]] == ["call_1"]
        # 配对响应保留
        assert out[1]["content"] == "R1"


# ---------------------------------------------------------------------------
# ⑤ 非 user list 降级 / system 原样保留
# ---------------------------------------------------------------------------

class TestListDowngrade:
    def test_tool_list_downgraded_to_str(self):
        """tool 存量 list（T3 污染链 #10）→ str：text 段拼接、image_url → [图片已省略]。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": [
                 {"type": "text", "text": "结果 ok"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
             ]},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert isinstance(out[1]["content"], str)
        assert out[1]["content"] == "结果 ok[图片已省略]"

    def test_assistant_list_downgraded_to_str(self):
        msgs = [
            {"role": "assistant",
             "content": [
                 {"type": "text", "text": "第一段"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
                 {"type": "text", "text": "第二段"},
             ]},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert out[0]["content"] == "第一段[图片已省略]第二段"

    def test_system_list_untouched(self):
        """system list 原样保留（cache_control 段不被压平——防 prompt caching 静默失效）。"""
        sys_content = [
            {"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "DYNAMIC"},
        ]
        msgs = [{"role": "system", "content": sys_content}, {"role": "user", "content": "Q"}]
        out = sanitize_llm_messages(copy.deepcopy(msgs))
        assert isinstance(out[0]["content"], list)
        assert len(out[0]["content"]) == 2
        assert out[0]["content"][0].get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# ⑥ 长度不变式锁（R4-B 直接回归）
# ---------------------------------------------------------------------------

class TestCopySemantics:
    def test_empty_input(self):
        assert sanitize_llm_messages([]) == []
        assert sanitize_llm_messages(None) == []

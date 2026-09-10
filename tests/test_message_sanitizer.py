"""发送层合规化 sanitize_llm_messages 单测（plan 2026-09-10-tool-message-order-fix §4）。

全 mock / 纯函数，禁真实 LLM。覆盖：
① 顺序规整：单/多 tool_calls、跨块响应、无 tool 零变化、ask_user DB 形态（3596-3598 复刻）、
   多 tool_calls + 图组合（合成 user 归位到 tool 块之后——执行序⑥）
② 幂等：无图输入恒等幂等（二次净化逐字节相同）；含图输入每请求确定性补全（同输入同输出）
③ 孤儿 tool 丢弃 / subagent_msg 丢弃 / 带图孤儿不产生合成 user（执行序②先于③ 回归锁）
④ 悬空 tool_calls 两形态：全悬空 → 整消息降级纯文本；部分悬空 → 按 tc 剥离、保留配对响应
⑤ 非 user list 降级（tool + assistant）/ system list 原样保留（cache_control 不被压平）
⑥ 图片合规化：user 展开 / assistant 不展开 / tool→合成 user / 零图段不发 / has_vision=False fail-closed
⑦ media type 正确性（PNG/JPG；HEIC + 非图片降级锁 T2 MIME 探测——plan §6）
⑧ 长度不变式锁：调用方 messages 对象零变化（副本语义）
"""

import copy
from pathlib import Path

import pytest

from agent.generic.message_sanitizer import sanitize_llm_messages

# ---------------------------------------------------------------------------
# 基建
# ---------------------------------------------------------------------------

def _tc(i, name="tool_x"):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _make_png(path, size=(8, 8), color=(255, 0, 0)):
    from PIL import Image
    img = Image.new("RGB", size, color)
    img.save(str(path), format="PNG")
    return Path(path)


def _make_jpg(path, size=(8, 8), color=(0, 255, 0)):
    from PIL import Image
    img = Image.new("RGB", size, color)
    img.save(str(path), format="JPEG")
    return Path(path)


def _image_blocks(content):
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]


# ---------------------------------------------------------------------------
# ① 顺序规整
# ---------------------------------------------------------------------------

class TestReorder:
    def test_single_tool_call_contiguous_unchanged(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert [e["role"] for e in out] == ["assistant", "tool"]
        assert out[0]["tool_calls"][0]["id"] == "call_1"

    def test_multi_tool_calls_contiguous_unchanged(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1), _tc(2)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
            {"role": "tool", "content": "R2", "tool_call_id": "call_2"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
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
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert [e["role"] for e in out] == ["assistant", "tool", "tool", "user"]
        assert [e.get("tool_call_id") for e in out[1:3]] == ["call_1", "call_2"]
        assert out[3]["content"] == "（补充说明）"

    def test_no_tools_zero_change(self):
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
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
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert [e["role"] for e in out] == ["assistant", "tool", "user"]
        assert out[1]["tool_call_id"] == "call_1"
        assert out[2]["content"] == "[用户回答] 继续"

    def test_multi_tool_calls_with_image_synth_user_after_block(self, tmp_path):
        """多 tool_calls + 图组合：合成 user 归位到 tool 块之后（执行序⑥）。"""
        p = _make_png(tmp_path / "s.png")
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "screenshot"), _tc(2)]},
            {"role": "tool", "content": f"![截图]({p})", "tool_call_id": "call_1"},
            {"role": "tool", "content": "R2 纯文本", "tool_call_id": "call_2"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert [e["role"] for e in out] == ["assistant", "tool", "tool", "user"]
        # tool1 保持 str（不展开）
        assert isinstance(out[1]["content"], str) and f"![截图]({p})" in out[1]["content"]
        # 合成 user 在 tool 块之后：text 说明段 + image_url 段
        synth = out[3]
        assert synth["content"][0] == {"type": "text", "text": "（以下是上一条工具结果中的图片）"}
        assert len(_image_blocks(synth["content"])) == 1

    def test_adjacent_assistant_gap_reorder(self):
        """相邻 assistant 缺口（中断/压缩重建形态）：A(tc c1) → B(纯文本) → tool(c1)
        → 全局配对归位为 A → tool(c1) → B（旧「扫描止于下一个 assistant」漏网，T1 QualityB P2）。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1)]},
            {"role": "assistant", "content": "B 纯文本"},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
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
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
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
        once = sanitize_llm_messages(copy.deepcopy(msgs), False)
        twice = sanitize_llm_messages(copy.deepcopy(once), False)
        assert twice == once

    def test_with_image_input_deterministic_per_request(self, tmp_path):
        """含图输入每请求确定性补全：同输入同输出（前缀缓存友好）。"""
        p = _make_png(tmp_path / "s.png")
        msgs = [
            {"role": "user", "content": f"看图 ![截图]({p})"},
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "screenshot")]},
            {"role": "tool", "content": f"![截图]({p})", "tool_call_id": "call_1"},
        ]
        out1 = sanitize_llm_messages(copy.deepcopy(msgs), True)
        out2 = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert out1 == out2


# ---------------------------------------------------------------------------
# ③ 丢弃：孤儿 tool / subagent_msg / 带图孤儿
# ---------------------------------------------------------------------------

class TestDiscard:
    def test_orphan_tool_dropped(self):
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "tool", "content": "ORPHAN", "tool_call_id": "call_none"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert all(e["role"] != "tool" for e in out)

    def test_subagent_msg_dropped(self):
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "subagent_msg", "content": "@ 前端展示消息"},
            {"role": "assistant", "content": "A"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert [e["role"] for e in out] == ["user", "assistant"]

    def test_image_orphan_tool_no_synth_user(self, tmp_path):
        """带图孤儿（压缩窗口切散的截图结果）→ 先丢弃、不产生合成 user（执行序②先于③ 回归锁）。"""
        p = _make_png(tmp_path / "s.png")
        msgs = [
            {"role": "user", "content": "Q"},
            {"role": "tool", "content": f"![截图]({p})", "tool_call_id": "call_orphan"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert [e["role"] for e in out] == ["user"]
        # 无合成 user（否则会出现「以下是上一条工具结果中的图片」文本）
        assert not any("以下是上一条工具结果中的图片" in str(e.get("content")) for e in out)


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
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert len(out) == 2
        assert "tool_calls" not in out[1]
        assert out[1]["content"] == "我在思考……"

    def test_all_dangling_empty_content_kept(self):
        """全悬空 + 空 content → 保留纯文本空消息（与 transform_history 既有语义一致）。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(9)]},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert out == [{"role": "assistant", "content": ""}]

    def test_partial_dangling_strip_by_tc(self):
        """部分悬空 → 按 tc 剥离悬空项、保留配对响应（不造新孤儿）。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1), _tc(2)]},
            {"role": "tool", "content": "R1", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
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
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
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
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert out[0]["content"] == "第一段[图片已省略]第二段"

    def test_system_list_untouched(self):
        """system list 原样保留（cache_control 段不被压平——防 prompt caching 静默失效）。"""
        sys_content = [
            {"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "DYNAMIC"},
        ]
        msgs = [{"role": "system", "content": sys_content}, {"role": "user", "content": "Q"}]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert isinstance(out[0]["content"], list)
        assert len(out[0]["content"]) == 2
        assert out[0]["content"][0].get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# ⑥ 图片合规化
# ---------------------------------------------------------------------------

class TestImageCompliance:
    def test_user_expands_markers(self, tmp_path):
        p = _make_png(tmp_path / "s.png")
        msgs = [{"role": "user", "content": f"看图 ![截图]({p}) 回答"}]
        out = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert isinstance(out[0]["content"], list)
        assert len(_image_blocks(out[0]["content"])) == 1

    def test_user_no_expand_without_vision(self, tmp_path):
        p = _make_png(tmp_path / "s.png")
        msgs = [{"role": "user", "content": f"看图 ![截图]({p})"}]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert out[0]["content"] == f"看图 ![截图]({p})"

    def test_assistant_not_expanded(self, tmp_path):
        """assistant 不展开（保留 str 文本标记；UI 渲染不受影响）。"""
        p = _make_png(tmp_path / "s.png")
        msgs = [{"role": "assistant", "content": f"这是人物 ![人物名]({p})"}]
        out = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert isinstance(out[0]["content"], str)
        assert f"![人物名]({p})" in out[0]["content"]

    def test_tool_image_becomes_synth_user(self, tmp_path):
        """tool 含图标记 → tool(str) + 紧跟合成 user(image_url)。"""
        p = _make_png(tmp_path / "s.png")
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "screenshot")]},
            {"role": "tool", "content": f"截图完成 ![截图]({p})", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert [e["role"] for e in out] == ["assistant", "tool", "user"]
        # tool content 恒 str（标记保留）
        assert isinstance(out[1]["content"], str)
        assert f"![截图]({p})" in out[1]["content"]
        # 合成 user：text 说明段 + image_url 段
        synth = out[2]
        assert synth["content"][0]["type"] == "text"
        assert "以下是上一条工具结果中的图片" in synth["content"][0]["text"]
        blocks = _image_blocks(synth["content"])
        assert len(blocks) == 1
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_zero_image_segments_no_synth_user(self, tmp_path):
        """零图段不发合成 user（文件缺失 → 仅保留文本标记，不产出误导性 text-only 合成消息）。"""
        missing = tmp_path / "missing.png"
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "screenshot")]},
            {"role": "tool", "content": f"截图完成 ![截图]({missing})", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), True)
        assert [e["role"] for e in out] == ["assistant", "tool"]
        assert isinstance(out[1]["content"], str)

    def test_has_vision_false_no_synth_user(self, tmp_path):
        """has_vision=False → fail-closed：文件存在也不展开、不发合成 user。"""
        p = _make_png(tmp_path / "s.png")
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "screenshot")]},
            {"role": "tool", "content": f"![截图]({p})", "tool_call_id": "call_1"},
        ]
        out = sanitize_llm_messages(copy.deepcopy(msgs), False)
        assert [e["role"] for e in out] == ["assistant", "tool"]


# ---------------------------------------------------------------------------
# ⑦ media type 正确性
# ---------------------------------------------------------------------------

class TestMediaType:
    def test_png_media_type(self, tmp_path):
        p = _make_png(tmp_path / "s.png")
        out = sanitize_llm_messages([{"role": "user", "content": f"![x]({p})"}], True)
        blocks = _image_blocks(out[0]["content"])
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_jpg_media_type(self, tmp_path):
        p = _make_jpg(tmp_path / "s.jpg")
        out = sanitize_llm_messages([{"role": "user", "content": f"![x]({p})"}], True)
        blocks = _image_blocks(out[0]["content"])
        assert blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_heic_media_type_by_payload(self, tmp_path):
        """HEIC：.heic 载荷（ISO BMFF ftyp+heic brand）→ data:image/heic（按魔数，不按扩展名）。"""
        p = tmp_path / "s.heic"
        p.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32)
        out = sanitize_llm_messages([{"role": "user", "content": f"![x]({p})"}], True)
        blocks = _image_blocks(out[0]["content"])
        assert len(blocks) == 1
        assert blocks[0]["image_url"]["url"].startswith("data:image/heic;base64,")

    def test_non_image_payload_degrades_to_text(self, tmp_path):
        """非图片载荷（假 .png 扩展名，文本内容）→ 不猜 media type：留标记原文 + [图片不可读] 警示。"""
        p = tmp_path / "fake.png"
        p.write_bytes(b"this is not an image payload at all")
        out = sanitize_llm_messages([{"role": "user", "content": f"![x]({p})"}], True)
        content = out[0]["content"]
        # 零图段（data URI 不产出）
        assert _image_blocks(content if isinstance(content, list) else []) == []
        text = content if isinstance(content, str) else "".join(
            b.get("text", "") for b in content if isinstance(b, dict))
        assert f"![x]({p})" in text
        assert "图片不可读" in text

    def test_mismatched_ext_png_payload_jpg_name(self, tmp_path):
        """扩展名与载荷不符：.jpg 文件实为 PNG 字节 → media type 跟载荷走（image/png）。"""
        p = _make_png(tmp_path / "s.jpg")
        out = sanitize_llm_messages([{"role": "user", "content": f"![x]({p})"}], True)
        blocks = _image_blocks(out[0]["content"])
        assert len(blocks) == 1
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# ⑧ 长度不变式锁（R4-B 直接回归）
# ---------------------------------------------------------------------------

class TestCopySemantics:
    def test_caller_list_unchanged(self, tmp_path):
        """sanitize 后调用方 messages 对象零变化（副本语义——不回流 DB/档/UI）。"""
        p = _make_png(tmp_path / "s.png")
        tool_content = f"![截图]({p})"
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc(1, "screenshot")]},
            {"role": "user", "content": "[用户回答] x"},
            {"role": "tool", "content": tool_content, "tool_call_id": "call_1"},
        ]
        snapshot = copy.deepcopy(msgs)
        out = sanitize_llm_messages(msgs, True)
        # 调用方列表零变化：条数、顺序、内容全同（合成 user 只出现在返回值）
        assert msgs == snapshot
        assert len(msgs) == 3
        assert all(isinstance(e.get("content"), str) for e in msgs)
        # 返回值含合成 user（与调用方列表相互独立）
        assert len(out) == 4
        assert out[3]["role"] == "user"

    def test_empty_input(self):
        assert sanitize_llm_messages([], True) == []
        assert sanitize_llm_messages(None, False) == []

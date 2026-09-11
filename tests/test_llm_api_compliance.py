"""LLM API 合规 wire 级断言（T2，plan 2026-09-10-tool-message-order-fix §4）。

mock litellm.completion 捕获**实际发出的请求体**，断言：
- 发送级：存量 list content（tool/assistant）→ wire 恒 str（image_url 段 → [图片已省略]）
- 消息 wire 形态：tool 键集={role,content,tool_call_id}（无 name）；
  tool/assistant content 恒 str；ask_user 形态顺序合规；无 subagent_msg role

全 mock，禁真实 LLM。
"""
from unittest.mock import patch


# ---------------------------------------------------------------------------
# ① 发送级：chat() wire 体 list content 降级
# ---------------------------------------------------------------------------

def _wire_messages(cfg, messages):
    """调用 LiteLLMSession.chat（mock litellm.completion 抛异常），返回 wire messages。"""
    from agent.generic.litellm_adapter import LiteLLMSession

    session = LiteLLMSession(cfg=cfg)
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")
        try:
            next(session.chat(messages=messages))
        except Exception:
            pass
        return mock_completion.call_args[1]["messages"]


def _base_cfg(**overrides):
    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
    }
    cfg.update(overrides)
    return cfg


def test_chat_wire_tool_list_content_downgraded_to_str():
    """存量 list content（tool）→ wire 恒 str（image_url 段 → [图片已省略]）。"""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": [
            {"type": "text", "text": "ok"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
        ]},
    ]
    out = _wire_messages(_base_cfg(), msgs)
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert isinstance(tool_msg["content"], str)
    assert "ok" in tool_msg["content"]


# ---------------------------------------------------------------------------
# ② 消息 wire 形态：ask_user 顺序 / subagent_msg / 键集 / content 类型
# ---------------------------------------------------------------------------

def test_chat_wire_ask_user_shape_compliance():
    """DB ask_user 形态 assistant(tc)→user(回答)→tool(结果) + subagent_msg →
    wire：assistant→tool 紧邻（k3-256k 400 根因形态）、subagent_msg 丢弃、
    tool 键集无 name、content 恒 str。"""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "ask_user", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "[主 Agent 回答] 继续"},
        {"role": "tool", "content": "[用户回答] 继续", "tool_call_id": "call_1"},
        {"role": "subagent_msg", "content": "@子名 消息（仅前端展示）"},
    ]
    out = _wire_messages(_base_cfg(), msgs)
    # 顺序合规：assistant(tool_calls) 后紧跟其 tool 响应；user 回答顺延到 tool 块之后
    assert [m["role"] for m in out] == ["assistant", "tool", "user"], (
        f"ask_user 形态必须规整为 assistant→tool→user，got: {[m['role'] for m in out]}"
    )
    # subagent_msg 不发出（@ 消息仅供前端展示）
    assert all(m["role"] != "subagent_msg" for m in out)
    # tool 键集 = {role, content, tool_call_id}（无 name——OpenAI 规范）
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert set(tool_msg.keys()) == {"role", "content", "tool_call_id"}
    # tool/assistant content 恒 str
    for m in out:
        if m["role"] in ("tool", "assistant"):
            assert isinstance(m["content"], str), f"{m['role']} content 必须为 str"


def test_chat_wire_assistant_list_content_downgraded():
    """存量 assistant list content → wire str（OpenAI 规范 assistant 仅 text/refusal 段，
    且 Niu 出站统一 str——UI/DB 不受影响）。"""
    msgs = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "已完成"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
        ]},
        {"role": "user", "content": "ok"},
    ]
    out = _wire_messages(_base_cfg(), msgs)
    assert isinstance(out[0]["content"], str)
    assert "已完成" in out[0]["content"]


# ---------------------------------------------------------------------------
# ③ LightRAG 关键词 schema strict 合规
# ---------------------------------------------------------------------------

def test_lightrag_keyword_schema_additional_properties_false():
    """OpenAI strict 模式硬要求：object schema 声明 additionalProperties:false。"""
    from niu_api.internal.lightrag_manager import _build_keyword_extraction_response_format

    rf = _build_keyword_extraction_response_format()
    assert rf["type"] == "json_schema"
    schema = rf["json_schema"]["schema"]
    assert schema.get("additionalProperties") is False, \
        "strict 模式缺 additionalProperties:false → OpenAI 路由必 400 后降级重发（每次白跑）"

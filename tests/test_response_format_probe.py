"""response_format 决策函数单元测试。

验证 _resolve_response_format 根据 litellm_kwargs.response_format_mode
决定构造哪种 response_format。这是纯函数，不调 LLM，仅检查配置。
"""
from niu_api.internal.lightrag_manager import (
    _resolve_response_format,
    _strip_response_format_mode,
)


def test_json_schema_mode_returns_json_schema_response_format():
    """response_format_mode=json_schema → 返回 json_schema strict 结构"""
    config = {"litellm_kwargs": {"response_format_mode": "json_schema"}}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "keyword_extraction"


def test_json_object_mode_returns_json_object_response_format():
    """response_format_mode=json_object → 返回 {"type": "json_object"}"""
    config = {"litellm_kwargs": {"response_format_mode": "json_object"}}
    rf = _resolve_response_format(config)
    assert rf == {"type": "json_object"}


def test_prompt_only_mode_returns_none():
    """response_format_mode=prompt_only → 返回 None（不构造）"""
    config = {"litellm_kwargs": {"response_format_mode": "prompt_only"}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_missing_mode_returns_none():
    """litellm_kwargs 无 response_format_mode 键 → 返回 None（保守降级）"""
    config = {"litellm_kwargs": {}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_missing_litellm_kwargs_returns_none():
    """配置无 litellm_kwargs 键 → 返回 None"""
    config = {}
    rf = _resolve_response_format(config)
    assert rf is None


def test_unknown_mode_returns_none():
    """response_format_mode 是未知值 → 返回 None（保守降级）"""
    config = {"litellm_kwargs": {"response_format_mode": "unknown_mode"}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_legacy_allowed_openai_params_still_supported():
    """旧配置只有 allowed_openai_params=["response_format"]（无 response_format_mode）
    → 兼容旧配置，返回 json_schema（默认最强档）

    Why: 旧版本用户配置文件没有 response_format_mode 字段，本次升级不应破坏。
    升级后首次启动后台探测会自动写入 response_format_mode。
    """
    config = {"litellm_kwargs": {"allowed_openai_params": ["response_format"]}}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_legacy_empty_allowed_openai_params_returns_none():
    """旧配置 allowed_openai_params=[]（无 response_format_mode）→ 返回 None"""
    config = {"litellm_kwargs": {"allowed_openai_params": []}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_json_schema_mode_with_thinking_kwargs():
    """豆包配置：response_format_mode=json_schema + thinking={type:disabled} 共存

    验证 thinking 参数不影响 response_format 决策。
    """
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_resolve_does_not_modify_config_no_side_effects():
    """_resolve_response_format 不修改 config（无副作用）

    Why: v4 曾用 pop 副作用修改 config，导致 keyword_extraction=True 与 False
    两种调用模式的 config_key 不一致，破坏 _get_litellm_session 缓存。
    v5 改为 get（无副作用），response_format_mode 字段在 _llm_model_func 内
    通过 _strip_response_format_mode 单独剔除。
    """
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    original = {"litellm_kwargs": dict(config["litellm_kwargs"])}
    _resolve_response_format(config)
    # config 不应被修改
    assert config["litellm_kwargs"] == original["litellm_kwargs"]
    assert "response_format_mode" in config["litellm_kwargs"]


def test_strip_response_format_mode_returns_new_dict_without_mode():
    """_strip_response_format_mode 返回新 dict，剔除 response_format_mode"""
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    stripped = _strip_response_format_mode(config)
    # 原config不修改
    assert "response_format_mode" in config["litellm_kwargs"]
    # 新config剔除 response_format_mode
    assert "response_format_mode" not in stripped["litellm_kwargs"]
    # 其他字段保留
    assert stripped["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert stripped["litellm_kwargs"]["allowed_openai_params"] == ["response_format"]


def test_strip_response_format_mode_returns_same_dict_when_no_mode():
    """无 response_format_mode 键时，_strip 返回原 config（不复制）"""
    config = {"litellm_kwargs": {"thinking": {"type": "disabled"}}}
    stripped = _strip_response_format_mode(config)
    # 无需复制，返回原对象
    assert stripped is config


def test_strip_response_format_mode_handles_missing_litellm_kwargs():
    """config 无 litellm_kwargs 键时，_strip 返回原 config"""
    config = {}
    stripped = _strip_response_format_mode(config)
    assert stripped is config


# 探测辅助函数单元测试：
# _classify_probe_response_tier1/tier2 根据响应文本判定 supported/gateway_blocked。
# _build_probe_messages 构造探测 prompt。
# _build_probe_response_format_json_schema / json_object 构造探测 response_format。
# 纯函数，不调真实 LLM。
from niu_api.compat import (
    _classify_probe_response_tier1,
    _classify_probe_response_tier2,
    _build_probe_messages,
    _build_probe_response_format_json_schema,
    _build_probe_response_format_json_object,
)


def test_build_probe_response_format_json_schema_structure():
    """json_schema 档构造 OpenAI Structured Outputs 标准结构"""
    rf = _build_probe_response_format_json_schema()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "probe_response_format"
    assert rf["json_schema"]["strict"] is True
    assert "ok" in rf["json_schema"]["schema"]["properties"]


def test_build_probe_response_format_json_object_structure():
    """json_object 档构造 {"type": "json_object"}"""
    rf = _build_probe_response_format_json_object()
    assert rf == {"type": "json_object"}


def test_build_probe_messages_returns_single_user_message_with_json_instruction():
    """探测消息含 JSON 字样（OpenAI json_object 模式硬性要求 prompt 含 'json'）"""
    msgs = _build_probe_messages()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "json" in msgs[0]["content"].lower()
    assert "ok" in msgs[0]["content"]


# Tier 1 (json_schema strict) 要求响应是合法 JSON dict + 含 ok 字段
def test_classify_tier1_supported_when_valid_json_with_ok_field():
    """响应是 {"ok": true} → supported"""
    assert _classify_probe_response_tier1('{"ok": true}') == "supported"


def test_classify_tier1_supported_when_json_with_extra_fields():
    """响应是 {"ok": true, "extra": "ignored"} → supported（schema strict 容忍额外字段）"""
    assert _classify_probe_response_tier1('{"ok": true, "extra": "ignored"}') == "supported"


def test_classify_tier1_gateway_blocked_when_plain_text():
    """响应是纯文本（如 GLM json_schema 实测输出 {"oko":）→ gateway_blocked"""
    assert _classify_probe_response_tier1('I am doing fine.') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_truncated_json():
    """响应是截断的非合法 JSON（如 GLM json_schema 实测 {"oko":）→ gateway_blocked"""
    assert _classify_probe_response_tier1('{"oko":') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier1('') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_markdown_wrapped():
    """响应是 ```json ...``` 包裹 → gateway_blocked（非纯 JSON）"""
    assert _classify_probe_response_tier1('```json\n{"ok": true}\n```') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_json_without_ok_field():
    """响应是 {"foo": "bar"}（合法 JSON 但无 ok 字段）→ gateway_blocked
    （json_schema strict 要求字段匹配，无 ok 说明 schema 未生效）"""
    assert _classify_probe_response_tier1('{"foo": "bar"}') == "gateway_blocked"


# Tier 2 (json_object) 不要求含 ok 字段，只要求合法 JSON dict
def test_classify_tier2_supported_when_valid_json_dict():
    """响应是 {"foo": "bar"}（合法 JSON dict，无 ok）→ supported
    json_object 不约束字段名，只要合法 JSON dict 即可"""
    assert _classify_probe_response_tier2('{"foo": "bar"}') == "supported"


def test_classify_tier2_supported_when_json_with_ok_field():
    """响应是 {"ok": true} → supported"""
    assert _classify_probe_response_tier2('{"ok": true}') == "supported"


def test_classify_tier2_gateway_blocked_when_plain_text():
    """响应是纯文本 → gateway_blocked"""
    assert _classify_probe_response_tier2('I am doing fine.') == "gateway_blocked"


def test_classify_tier2_gateway_blocked_when_truncated_json():
    """响应是 {"ok": true}\\n} （GLM json_object 实测含额外字符）→ gateway_blocked"""
    assert _classify_probe_response_tier2('{"ok": true}\n}') == "gateway_blocked"


def test_classify_tier2_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier2('') == "gateway_blocked"


"""升级后自动探测决策函数测试。

_should_auto_probe_after_upgrade 判断 lightrag_llm.litellm_kwargs 是否需要自动探测：
- 无 response_format_mode 键 → True（旧版本配置，需探测）
- 有 response_format_mode 键 → False（已探测过）

v6 修正：同时检查 lightrag_llm 和 llm 两段 litellm_kwargs，因为
lightrag_llm.model 为空时 get_llm_config 走 fallback 用 llm 段，
response_format_mode 可能写在 llm 段（场景二/三：LightRAG 用主 Agent 同一模型）。
"""
from niu_api.internal.lightrag_manager import _should_auto_probe_after_upgrade


def test_returns_true_when_no_response_format_mode():
    """lightrag_llm.litellm_kwargs 无 response_format_mode 键 → True（旧版本）"""
    config = {"lightrag_llm": {"litellm_kwargs": {"thinking": {"type": "disabled"}}}}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_true_when_no_litellm_kwargs():
    """lightrag_llm 无 litellm_kwargs 键 → True"""
    config = {"lightrag_llm": {"reasoning_effort": "none"}}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_true_when_no_lightrag_llm():
    """配置无 lightrag_llm 段 → True"""
    config = {}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_false_when_response_format_mode_exists():
    """lightrag_llm.litellm_kwargs 含 response_format_mode 键 → False（已探测过）"""
    config = {"lightrag_llm": {"litellm_kwargs": {"response_format_mode": "prompt_only"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_false_when_response_format_mode_is_any_value():
    """response_format_mode 是任意值（含 prompt_only）都算已探测过 → False"""
    config = {"lightrag_llm": {"litellm_kwargs": {"response_format_mode": "json_schema"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_false_when_llm_has_response_format_mode():
    """llm.litellm_kwargs 含 response_format_mode（lightrag_llm 为空场景）→ False

    场景二/三：LightRAG 用主 Agent 同一模型，response_format_mode 写在 llm 段。
    """
    config = {"llm": {"litellm_kwargs": {"response_format_mode": "prompt_only"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_true_when_only_llm_has_litellm_kwargs_without_mode():
    """llm.litellm_kwargs 有内容但无 response_format_mode 键 → True（需探测）"""
    config = {"llm": {"litellm_kwargs": {"thinking": {"type": "enabled"}}}}
    assert _should_auto_probe_after_upgrade(config) is True
